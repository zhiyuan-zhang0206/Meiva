"use client";

// /control#display — user-facing display & behavior preferences.
//
// Each setting is a row: label + description + control (toggle for bool,
// radio group for enums). Changes are optimistic — the toggle flips
// immediately while a PUT fires in the background; on failure it reverts.
//
// Settings are stored server-side (user_settings DB table) so they survive
// across browsers and machines.

import {
  Activity,
  AlertTriangle,
  Bell,
  Calendar,
  CalendarClock,
  CircleDot,
  Clock,
  EyeOff,
  Languages,
  MessageSquareText,
  MoveHorizontal,
  Palette,
} from "lucide-react";
import { cn } from "@/lib/format/utils";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { DEFAULT_DATE_FORMAT } from "@/lib/contracts/types";
import { groupedModels, isSuperseded } from "@/lib/contracts/models";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import {
  TIMELINE_NARROW_BREAKPOINT_PX,
  TIMELINE_WIDTH_PX_DEFAULT,
  TIMELINE_WIDTH_PX_MAX,
  TIMELINE_WIDTH_PX_MIN,
  TIMELINE_WIDTH_PX_STEP,
} from "@/lib/timeline/timeline-width";
import { themePackId, useThemePacks } from "@/lib/plugins/use-theme-packs";
import { readStoredSetting, useDebouncedSetting, useUserSettings } from "@/lib/state/use-user-settings";
import { FLEX } from "@/lib/layout/layout";
import { ModelPickerSection } from "./_model_picker";
import { NotificationCategoryRows } from "./_notification-categories";
import { RadioRow, SettingsSection, SliderRow, ToggleRow } from "./_rows";
import { useApi } from "@/lib/transport/transport-context";

// ── Page ──

export default function DisplaySettingsPage() {
  const client = useApi();
  const t = useTranslations("displaySettings");
  const { settings, setSetting, isLoading } = useUserSettings();
  const [widthPx, setWidthPx] = useDebouncedSetting<number>(
    "display.timeline_width_px",
    TIMELINE_WIDTH_PX_DEFAULT,
  );
  // Task #805: the width ratio is a desktop concept — on narrow viewports
  // (below md, phones) the timeline is full-width and the setting is inert,
  // so the slider is disabled here rather than promising an effect it won't
  // have. Same 768px boundary the layout itself uses.
  const { isNarrow } = useBreakpoint();

  // Skins contributed by the cluster's enabled plugins. Must be called
  // unconditionally (before any early return) — React hook rules.
  const { packs: themePacks } = useThemePacks();

  // Fetch models for the model picker visibility section.
  // Must be called unconditionally (before any early return) — React hook rules.
  const { data: modelsData } = useQuery({
    queryKey: ["models"],
    queryFn: () => client.getModels(),
    staleTime: Infinity,
  });

  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <div className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  const hiddenModels: string[] = readStoredSetting<string[]>(settings["models.hidden"], []);
  // meiva:note
  // A provider switch turns its whole family off without touching the per-model
  // rows: the models keep the state the user gave them, and while the provider is
  // off those rows are not editable (user 2026-10-20). The spawn picker reads the
  // same two lists, so an off provider contributes no models there either.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/model-presentation.meiva.okf.md
  const hiddenProviders: string[] = readStoredSetting<string[]>(settings["models.hidden_providers"], []);
  // meiva:note
  // A superseded model is not a toggle: the spawn picker already refuses it, so a
  // switch here would change nothing a spawn would read (user 2026-10-20: entries
  // that occupy space and do nothing). It is dropped from the list rather than
  // shown disabled with an explanation.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability/model-presentation.meiva.okf.md
  const modelGroups = groupedModels(modelsData, (m) => !isSuperseded(modelsData, m));

  return (
    <div className="space-y-6">
      <SettingsSection id="display-language" title={t("language")}>
        <RadioRow
          icon={Languages}
          label={t("language")}
          description={t("languageDescription")}
          options={[
            { value: "en", label: t("english") },
            { value: "zh", label: t("chinese") },
          ]}
          value={readStoredSetting(settings["display.language"], "en")}
          onChange={(v) => setSetting("display.language", v)}
        />
      </SettingsSection>

      {/* Skins exist only when a plugin contributes one, so the section
          appears with the first pack rather than standing empty offering the
          console's own palette as the only choice. */}
      {themePacks.length > 0 && (
        <SettingsSection id="display-theme" title="Theme">
          <RadioRow
            icon={Palette}
            label="Skin"
            description="A plugin-contributed color pack, applied over the active light/dark palette. Colors it does not set keep following light/dark. A pack marked 'no dark variant' pins the colors it does set across both modes, so the light/dark toggle will not change them"
            options={[
              { value: "", label: "Default (Meiva)" },
              ...themePacks.map((p) => ({
                value: themePackId(p),
                label: `${p.name} (${p.plugin})${p.dark_tokens ? "" : " — no dark variant"}`,
              })),
            ]}
            value={readStoredSetting(settings["display.theme_pack"], "")}
            onChange={(v) => setSetting("display.theme_pack", v === "" ? null : v)}
          />
        </SettingsSection>
      )}

      <SettingsSection id="display-agent-list" title={t("agentListDisplay")}>
        <ToggleRow
          icon={CircleDot}
          label="Show agent status dot"
          description="Show a status color dot (running/idle/…) on each sidebar agent row; off by default to keep the list static and quiet"
          value={settings["display.show_agent_status"] === true}
          onChange={(v) => setSetting("display.show_agent_status", v)}
        />
        <ToggleRow
          icon={EyeOff}
          label="Show terminated agents"
          description="Show terminated agents (red status dot) in the sidebar; off shows only live agents"
          value={settings["display.show_terminated"] === true}
          onChange={(v) => setSetting("display.show_terminated", v)}
        />
        <ToggleRow
          icon={CalendarClock}
          label="Show weekday in timestamps"
          description="Show the weekday in timeline card timestamps (e.g. [2026-05-06 Tue 16:56:29])"
          value={settings["display.show_timestamp_weekday"] === true}
          onChange={(v) => setSetting("display.show_timestamp_weekday", v)}
        />
        <RadioRow
          icon={Clock}
          label="Clock format"
          description="How clock times are written across the console: 24-hour (15:45) or 12-hour (3:45 PM). Default 24-hour"
          options={[
            { value: "24h", label: "24-hour" },
            { value: "12h", label: "12-hour" },
          ]}
          value={settings["display.clock_format"] === "12h" ? "12h" : "24h"}
          onChange={(v) => setSetting("display.clock_format", v)}
        />
        <RadioRow
          icon={Clock}
          label="Time display mode"
          description="The time shown at the right of each agent row"
          options={[
            { value: "last_active", label: "Last active" },
            { value: "spawned", label: "Spawned" },
            { value: "hidden", label: "Hidden" },
          ]}
          value={readStoredSetting(settings["display.time_mode"], "last_active")}
          onChange={(v) => setSetting("display.time_mode", v)}
        />
        <RadioRow
          icon={Calendar}
          label="Date format"
          description="Display format for timestamps"
          options={[
            { value: "relative", label: "Relative (e.g. 3m ago)" },
            { value: "absolute", label: "Absolute (e.g. 7/12 15:45)" },
          ]}
          value={readStoredSetting(settings["display.date_format"], DEFAULT_DATE_FORMAT)}
          onChange={(v) => setSetting("display.date_format", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-timeline" title="Timeline">
        <SliderRow
          icon={MoveHorizontal}
          label="Timeline max width"
          description={`Max width of the conversation column in pixels (${TIMELINE_WIDTH_PX_MIN}px–${TIMELINE_WIDTH_PX_MAX}px). The column fills its pane up to this ceiling and centers with side gutters when wider. Desktop only: on phones (below ${TIMELINE_NARROW_BREAKPOINT_PX}px) the timeline is full-width and this setting is ignored. Default ${TIMELINE_WIDTH_PX_DEFAULT}px.`}
          min={TIMELINE_WIDTH_PX_MIN}
          max={TIMELINE_WIDTH_PX_MAX}
          step={TIMELINE_WIDTH_PX_STEP}
          value={widthPx}
          onChange={setWidthPx}
          format={(v) => `${v}px`}
          disabled={isNarrow}
        />
      </SettingsSection>

      <SettingsSection id="display-notifications" title="Notifications">
        <ToggleRow
          icon={Bell}
          label="Awaiting-reply notification"
          description="Show a red dot + count badge in the sidebar for agents awaiting reply; off by default — check the Fleet page when you want to know"
          value={settings["notification.awaiting_reply"] === true}
          onChange={(v) => setSetting("notification.awaiting_reply", v)}
        />
        <NotificationCategoryRows />
      </SettingsSection>

      <SettingsSection id="display-realtime" title="Realtime connection">
        <ToggleRow
          icon={Activity}
          label="Single realtime connection"
          description="Carry the agent list, the open conversation and alerts over one stream instead of three. Off keeps one stream per channel, exactly as before; on frees connections for the page itself, which matters most on plain HTTP where a browser allows six per origin."
          value={settings["web.sseSingleStream"] === true}
          onChange={(v) => setSetting("web.sseSingleStream", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-composer" title={t("composer")}>
        <ToggleRow
          icon={MessageSquareText}
          label={t("replySuggestions")}
          description={t("replySuggestionsDesc")}
          value={settings["behavior.replySuggestions"] === true}
          onChange={(v) => setSetting("behavior.replySuggestions", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-confirmations" title="Confirmations">
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before terminate"
          description="Show a confirmation dialog when terminating an agent"
          value={settings["behavior.confirm_terminate"] === true}
          onChange={(v) => setSetting("behavior.confirm_terminate", v)}
        />
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before restart"
          description="Show a confirmation dialog when restarting an agent"
          value={settings["behavior.confirm_restart"] === true}
          onChange={(v) => setSetting("behavior.confirm_restart", v)}
        />
        <ToggleRow
          icon={AlertTriangle}
          label="Confirm before force kill"
          description="Show a confirmation dialog when force-killing (kill / SIGKILL) an agent"
          value={settings["behavior.confirm_force_kill"] === true}
          onChange={(v) => setSetting("behavior.confirm_force_kill", v)}
        />
      </SettingsSection>

      <SettingsSection id="display-model-picker" title="Model picker">
        <ModelPickerSection
          groups={modelGroups}
          models={modelsData?.models}
          hiddenModels={hiddenModels}
          hiddenProviders={hiddenProviders}
          onToggleModel={(modelName, on) =>
            setSetting(
              "models.hidden",
              on ? hiddenModels.filter((m) => m !== modelName) : [...hiddenModels, modelName],
            )
          }
          onToggleProvider={(provider, on) =>
            setSetting(
              "models.hidden_providers",
              on
                ? hiddenProviders.filter((p) => p !== provider)
                : [...hiddenProviders, provider],
            )
          }
        />
      </SettingsSection>
    </div>
  );
}
