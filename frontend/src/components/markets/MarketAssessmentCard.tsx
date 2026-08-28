import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Sparkles } from "lucide-react";
import { api, type MarketAssessmentResponse } from "@/lib/api";
import { marketInsightToneClass } from "@/lib/market-colors";
import { cn } from "@/lib/utils";

type CardState = "idle" | "loading" | "ready" | "error";

function stanceTone(stance: MarketAssessmentResponse["stance"]): "up" | "down" | "neutral" {
  if (stance === "bullish") return "up";
  if (stance === "bearish") return "down";
  return "neutral";
}

function BulletList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      <ul className="mt-1.5 space-y-1 text-sm leading-relaxed">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <span aria-hidden className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted-foreground/60" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MarketAssessmentCard({ symbol, name }: { symbol: string; name?: string }) {
  const { t } = useTranslation();
  const [state, setState] = useState<CardState>("idle");
  const [assessment, setAssessment] = useState<MarketAssessmentResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setState("idle");
    setAssessment(null);
    setError(null);
  }, [symbol]);

  const generate = useCallback(async () => {
    if (!symbol) return;
    setState("loading");
    setError(null);
    try {
      const payload = await api.generateMarketAssessment(symbol);
      setAssessment(payload);
      setState("ready");
    } catch (err) {
      const message = err instanceof Error ? err.message : t("markets.assessment.error");
      setError(message);
      setState("error");
    }
  }, [symbol, t]);

  const stance = assessment?.stance;
  const confidence = assessment?.confidence;

  return (
    <section className="rounded-xl border border-primary/15 bg-primary/5 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium text-primary">{t("markets.assessment.title")}</div>
          {state === "idle" ? (
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              {t("markets.assessment.idleHint", { name: name || symbol })}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => void generate()}
          disabled={state === "loading"}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-xs font-medium shadow-sm transition",
            "hover:border-primary/40 hover:text-primary disabled:cursor-not-allowed disabled:opacity-60",
          )}
        >
          {state === "loading" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
          )}
          {state === "loading" ? t("markets.assessment.generating") : t("markets.assessment.generate")}
        </button>
      </div>

      {state === "error" && error ? (
        <p className="mt-3 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      {state === "ready" && assessment ? (
        <div className="mt-3 space-y-3">
          {assessment.fallback ? (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
              {t("markets.assessment.fallbackHint")}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            {stance ? (
              <span
                className={cn(
                  "inline-flex rounded-full px-2 py-0.5 text-xs font-medium",
                  marketInsightToneClass(stanceTone(stance)),
                )}
              >
                {t(`markets.assessment.stance.${stance}`)}
              </span>
            ) : null}
            {confidence ? (
              <span className="text-xs text-muted-foreground">
                {t("markets.assessment.confidenceLabel", {
                  level: t(`markets.assessment.confidence.${confidence}`),
                })}
              </span>
            ) : null}
            {assessment.source === "llm" ? (
              <span className="text-xs text-muted-foreground">{t("markets.assessment.sourceLlm")}</span>
            ) : null}
          </div>

          {assessment.headline ? (
            <p className="text-base font-semibold leading-snug text-foreground">{assessment.headline}</p>
          ) : null}
          {assessment.summary ? (
            <p className="text-sm leading-relaxed text-foreground">{assessment.summary}</p>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-3">
            <BulletList title={t("markets.assessment.drivers")} items={assessment.drivers ?? []} />
            <BulletList title={t("markets.assessment.risks")} items={assessment.risks ?? []} />
            <BulletList title={t("markets.assessment.catalysts")} items={assessment.catalysts ?? []} />
          </div>
        </div>
      ) : null}
    </section>
  );
}
