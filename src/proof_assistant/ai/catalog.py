"""Static driver metadata and conservative, explicitly non-live fallbacks."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    Difficulty,
    DriverId,
    DriverTransport,
    ModelDescriptor,
)

_OPENAI_DIFFICULTIES = (
    Difficulty.AUTO,
    Difficulty.NONE,
    Difficulty.LOW,
    Difficulty.MEDIUM,
    Difficulty.HIGH,
    Difficulty.XHIGH,
    Difficulty.MAX,
)
_CLAUDE_DIFFICULTIES = (
    Difficulty.AUTO,
    Difficulty.LOW,
    Difficulty.MEDIUM,
    Difficulty.HIGH,
    Difficulty.XHIGH,
    Difficulty.MAX,
)
_COPILOT_DIFFICULTIES = _CLAUDE_DIFFICULTIES
_GEMINI_DIFFICULTIES = (
    Difficulty.AUTO,
    Difficulty.LOW,
    Difficulty.MEDIUM,
    Difficulty.HIGH,
)


@dataclass(frozen=True, slots=True)
class DriverDefinition:
    driver: DriverId
    transport: DriverTransport
    display_name: str
    executable: str | None
    version_argv: tuple[str, ...]
    auth_argv: tuple[str, ...]
    credential_environment_variable: str | None
    api_models_url: str | None
    difficulties: tuple[Difficulty, ...]
    curated_models: tuple[ModelDescriptor, ...]
    install_npm_package: str | None
    login_instruction: str


def _model(
    model_id: str,
    difficulties: tuple[Difficulty, ...],
    display_name: str | None = None,
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        display_name=display_name or model_id,
        difficulties=difficulties,
    )


DRIVER_DEFINITIONS: dict[DriverId, DriverDefinition] = {
    DriverId.CODEX_CLI: DriverDefinition(
        driver=DriverId.CODEX_CLI,
        transport=DriverTransport.CLI,
        display_name="OpenAI Codex CLI",
        executable="codex",
        version_argv=("codex", "--version"),
        auth_argv=("codex", "login", "status"),
        credential_environment_variable=None,
        api_models_url=None,
        difficulties=_OPENAI_DIFFICULTIES,
        curated_models=(
            _model("gpt-5.6-sol", _OPENAI_DIFFICULTIES, "GPT-5.6 Sol"),
            _model("gpt-5.6-terra", _OPENAI_DIFFICULTIES, "GPT-5.6 Terra"),
            _model("gpt-5.6-luna", _OPENAI_DIFFICULTIES, "GPT-5.6 Luna"),
        ),
        install_npm_package="@openai/codex",
        login_instruction="Run `codex login` (or `codex login --device-auth` remotely).",
    ),
    DriverId.CLAUDE_CLI: DriverDefinition(
        driver=DriverId.CLAUDE_CLI,
        transport=DriverTransport.CLI,
        display_name="Anthropic Claude Code CLI",
        executable="claude",
        version_argv=("claude", "--version"),
        auth_argv=("claude", "auth", "status", "--text"),
        credential_environment_variable=None,
        api_models_url=None,
        difficulties=_CLAUDE_DIFFICULTIES,
        curated_models=(
            _model("opus", _CLAUDE_DIFFICULTIES, "Claude Opus (account alias)"),
            _model("sonnet", _CLAUDE_DIFFICULTIES, "Claude Sonnet (account alias)"),
            _model("haiku", _CLAUDE_DIFFICULTIES, "Claude Haiku (account alias)"),
        ),
        install_npm_package="@anthropic-ai/claude-code",
        login_instruction="Run `claude auth login` and complete the account flow.",
    ),
    DriverId.COPILOT_CLI: DriverDefinition(
        driver=DriverId.COPILOT_CLI,
        transport=DriverTransport.CLI,
        display_name="GitHub Copilot CLI",
        executable="copilot",
        version_argv=("copilot", "version"),
        auth_argv=(),
        credential_environment_variable=None,
        api_models_url=None,
        difficulties=_COPILOT_DIFFICULTIES,
        curated_models=(
            _model("auto", _COPILOT_DIFFICULTIES, "Automatic (account policy)"),
        ),
        install_npm_package="@github/copilot",
        login_instruction=(
            "Run `copilot login`; Proof Assistant cannot noninteractively verify "
            "Copilot entitlement without making a billable model request."
        ),
    ),
    DriverId.OPENAI_API: DriverDefinition(
        driver=DriverId.OPENAI_API,
        transport=DriverTransport.API,
        display_name="OpenAI API",
        executable=None,
        version_argv=(),
        auth_argv=(),
        credential_environment_variable="OPENAI_API_KEY",
        api_models_url="https://api.openai.com/v1/models",
        difficulties=_OPENAI_DIFFICULTIES,
        curated_models=(
            _model("gpt-5.6-sol", _OPENAI_DIFFICULTIES, "GPT-5.6 Sol"),
            _model("gpt-5.6-terra", _OPENAI_DIFFICULTIES, "GPT-5.6 Terra"),
            _model("gpt-5.6-luna", _OPENAI_DIFFICULTIES, "GPT-5.6 Luna"),
        ),
        install_npm_package=None,
        login_instruction="Set OPENAI_API_KEY or store a key in the configured credential store.",
    ),
    DriverId.ANTHROPIC_API: DriverDefinition(
        driver=DriverId.ANTHROPIC_API,
        transport=DriverTransport.API,
        display_name="Anthropic API",
        executable=None,
        version_argv=(),
        auth_argv=(),
        credential_environment_variable="ANTHROPIC_API_KEY",
        api_models_url="https://api.anthropic.com/v1/models",
        difficulties=_CLAUDE_DIFFICULTIES,
        curated_models=(
            _model("claude-opus-4-6", _CLAUDE_DIFFICULTIES, "Claude Opus 4.6"),
            _model("claude-sonnet-4-6", _CLAUDE_DIFFICULTIES, "Claude Sonnet 4.6"),
            _model("claude-haiku-4-5", _CLAUDE_DIFFICULTIES, "Claude Haiku 4.5"),
        ),
        install_npm_package=None,
        login_instruction=(
            "Set ANTHROPIC_API_KEY or store a key in the configured credential store."
        ),
    ),
    DriverId.GEMINI_API: DriverDefinition(
        driver=DriverId.GEMINI_API,
        transport=DriverTransport.API,
        display_name="Google Gemini API",
        executable=None,
        version_argv=(),
        auth_argv=(),
        credential_environment_variable="GEMINI_API_KEY",
        api_models_url="https://generativelanguage.googleapis.com/v1beta/models",
        difficulties=_GEMINI_DIFFICULTIES,
        curated_models=(
            _model("gemini-2.5-pro", _GEMINI_DIFFICULTIES, "Gemini 2.5 Pro"),
            _model("gemini-2.5-flash", _GEMINI_DIFFICULTIES, "Gemini 2.5 Flash"),
        ),
        install_npm_package=None,
        login_instruction="Set GEMINI_API_KEY or store a key in the configured credential store.",
    ),
}


def driver_definition(driver: DriverId) -> DriverDefinition:
    return DRIVER_DEFINITIONS[driver]
