"""Custom exceptions for the Slack analytics bot."""


class SlackBotError(Exception):
    """Base exception for all Slack bot errors."""


class SlackSignatureError(SlackBotError):
    """Raised when Slack request signature verification fails."""


class SlackEventParseError(SlackBotError):
    """Raised when a Slack event payload cannot be parsed."""


class SlackNormalizationError(SlackBotError):
    """Raised when a Slack message cannot be normalized to meaningful text."""


class SlackDeliveryError(SlackBotError):
    """Raised when Slack message delivery fails."""
