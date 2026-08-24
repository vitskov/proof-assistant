"""Centralized light and dark color systems for the terminal interface.

The palette is based on Flexoki's warm ink-and-paper colors.  Semantic tokens
keep layout CSS independent of individual hex values and ensure panels,
callouts, focus states, and chrome change together when the theme changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class ProofPalette:
    name: str
    dark: bool
    primary: str
    secondary: str
    warning: str
    error: str
    success: str
    accent: str
    foreground: str
    background: str
    surface: str
    panel: str
    muted: str
    subtle_border: str
    focus: str
    cursor_foreground: str
    callout_warning_background: str
    callout_warning_text: str
    callout_error_background: str
    callout_error_text: str
    callout_success_background: str
    callout_success_text: str
    callout_info_background: str
    callout_info_text: str

    def variables(self) -> dict[str, str]:
        return {
            "text-muted": self.muted,
            "foreground-muted": self.muted,
            "border": self.focus,
            "border-blurred": self.subtle_border,
            "block-cursor-background": self.focus,
            "block-cursor-foreground": self.cursor_foreground,
            "block-cursor-text-style": "bold",
            "input-cursor-background": self.focus,
            "input-cursor-foreground": self.cursor_foreground,
            "input-selection-background": f"{self.focus} 35%",
            "scrollbar": self.subtle_border,
            "scrollbar-hover": self.focus,
            "scrollbar-active": self.focus,
            "scrollbar-background": self.background,
            "button-color-foreground": "#FFFCF0",
            "button-focus-text-style": "bold reverse",
            "footer-background": self.panel,
            "footer-foreground": self.foreground,
            "footer-key-foreground": self.focus,
            "footer-key-background": "transparent",
            "footer-description-foreground": self.muted,
            "footer-description-background": "transparent",
            "proof-page-background": self.background,
            "proof-panel-background": self.panel,
            "proof-input-background": self.surface,
            "proof-dialog-background": self.surface,
            "proof-code-background": self.surface,
            "proof-chrome-background": self.panel,
            "proof-chrome-foreground": self.foreground,
            "proof-muted": self.muted,
            "proof-panel-border": self.subtle_border,
            "proof-focus": self.focus,
            "proof-warning-background": self.callout_warning_background,
            "proof-warning-text": self.callout_warning_text,
            "proof-error-background": self.callout_error_background,
            "proof-error-text": self.callout_error_text,
            "proof-success-background": self.callout_success_background,
            "proof-success-text": self.callout_success_text,
            "proof-info-background": self.callout_info_background,
            "proof-info-text": self.callout_info_text,
            "proof-overlay": f"{self.background} 78%",
        }

    def theme(self) -> Theme:
        return Theme(
            name=self.name,
            primary=self.primary,
            secondary=self.secondary,
            warning=self.warning,
            error=self.error,
            success=self.success,
            accent=self.accent,
            foreground=self.foreground,
            background=self.background,
            surface=self.surface,
            panel=self.panel,
            dark=self.dark,
            text_alpha=1.0,
            variables=self.variables(),
        )


PROOF_DARK_PALETTE = ProofPalette(
    name="proof-ink",
    dark=True,
    primary="#205EA6",
    secondary="#1C6C66",
    warning="#9D4310",
    error="#AF3029",
    success="#536907",
    accent="#5E409D",
    foreground="#CECDC3",
    background="#100F0F",
    surface="#1C1B1A",
    panel="#282726",
    muted="#B7B5AC",
    subtle_border="#878580",
    focus="#4385BE",
    cursor_foreground="#100F0F",
    callout_warning_background="#27180E",
    callout_warning_text="#F9AE77",
    callout_error_background="#261312",
    callout_error_text="#F89A8A",
    callout_success_background="#1A1E0C",
    callout_success_text="#BEC97E",
    callout_info_background="#101A24",
    callout_info_text="#92BFDB",
)

PROOF_LIGHT_PALETTE = ProofPalette(
    name="proof-paper",
    dark=False,
    primary="#205EA6",
    secondary="#1C6C66",
    warning="#9D4310",
    error="#AF3029",
    success="#536907",
    accent="#5E409D",
    foreground="#343331",
    background="#FFFCF0",
    surface="#F2F0E5",
    panel="#E6E4D9",
    muted="#575653",
    subtle_border="#6F6E69",
    focus="#205EA6",
    cursor_foreground="#FFFCF0",
    callout_warning_background="#FFE7CE",
    callout_warning_text="#71320D",
    callout_error_background="#FFE1D5",
    callout_error_text="#6C201C",
    callout_success_background="#EDEECF",
    callout_success_text="#3D4C07",
    callout_info_background="#E1ECEB",
    callout_info_text="#163B66",
)

PROOF_DARK_THEME = PROOF_DARK_PALETTE.theme()
PROOF_LIGHT_THEME = PROOF_LIGHT_PALETTE.theme()
PROOF_THEMES = (PROOF_DARK_THEME, PROOF_LIGHT_THEME)
DEFAULT_PROOF_THEME = PROOF_DARK_THEME.name

# CSS is parsed before ``on_mount`` registers the custom themes.  Supplying the
# dark semantic tokens as defaults keeps parsing deterministic; activation of a
# registered theme immediately replaces them.
THEME_VARIABLE_DEFAULTS = PROOF_DARK_PALETTE.variables()
