from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from base.config._full import (
        AgentSettings,
        AlertsSettings,
        DaemonSettings,
        DataPlaneSettings,
        DisplaySettings,
        FeishuSettings,
        GatewaySettings,
        GeneralSettings,
        LmSettings,
        ObservabilitySettings,
        PackagesSettings,
        SandboxSettings,
        ServiceSettings,
        TelegramSettings,
        ToUserSettings,
        TransferSettings,
        WalgSettings,
        WebSettings,
    )
    from base.host.env.dotenv_boot import EnvBootResult


# meiva:note
# The typed shape of `ConfigBoot.view`: one annotation per config domain, inherited by `_SettingsView`.
# The annotations declare no attribute, so `__getattr__` keeps serving every domain at runtime: the real
# sub-model once the process is full, a read proxy while it is lite. A lite process must not import the
# sub-models (pydantic_settings), so the classes are imported for the type checker only. This list is the
# single copy of the domain set outside `Settings`; a test pins it to the aggregate's fields.
class SettingsDomains:
    __slots__ = ()

    env_boot: EnvBootResult
    lm: LmSettings
    alerts: AlertsSettings
    sandbox: SandboxSettings
    agent: AgentSettings
    web: WebSettings
    to_user: ToUserSettings
    transfer: TransferSettings
    gateway: GatewaySettings
    daemon: DaemonSettings
    data_plane: DataPlaneSettings
    services: ServiceSettings
    observability: ObservabilitySettings
    display: DisplaySettings
    packages: PackagesSettings
    feishu: FeishuSettings
    telegram: TelegramSettings
    walg: WalgSettings
    general: GeneralSettings
