"""The Discord end of the gate: posting requests, and turning clicks into answers.

Two halves, kept apart on purpose. `DiscordBridge` is the logic -- who may
answer, what a click means, what the message should say next -- and takes the
API it calls as a constructor argument, so all of it is testable without a
socket. `packages/remote/discord_client.py` is the half that talks to Discord.

The authority question runs through everything here. A button in this channel
approves a command that ANA's rule set already decided was worth stopping, so
an interaction is answered only when it comes from the configured channel *and*
from a user id on the allowlist. Anything else is refused to the clicker's face
and never touches the pending future.
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional

from packages.remote import render

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordConfig:
    token: str
    channel_id: str
    allowed_user_ids: frozenset
    turn_summaries: bool = True

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> Optional["DiscordConfig"]:
        """None when the bridge is not configured, which is the off switch: no
        client is started and the gate keeps its old single-door behavior."""
        env = os.environ if env is None else env
        token = (env.get("ANA_DISCORD_BOT_TOKEN") or "").strip()
        channel_id = (env.get("ANA_DISCORD_CHANNEL_ID") or "").strip()
        if not token or not channel_id:
            return None
        # Singular accepted as well: one person answering their own agent is
        # the common case, and "…_USER_ID" is what that person writes.
        raw = env.get("ANA_DISCORD_ALLOWED_USER_IDS") or env.get("ANA_DISCORD_ALLOWED_USER_ID") or ""
        return cls(
            token=token,
            channel_id=channel_id,
            allowed_user_ids=frozenset(part.strip() for part in raw.split(",") if part.strip()),
            turn_summaries=(env.get("ANA_DISCORD_TURN_SUMMARIES", "on") or "").lower() != "off",
        )

    def may_answer(self, user_id: str) -> bool:
        """An empty allowlist means nobody, not everybody.

        The other reading would turn "I set the token but not the ids" into
        "anyone in the server can approve `rm -rf`"."""
        return bool(user_id) and user_id in self.allowed_user_ids


@dataclass
class _Posted:
    """One request as it currently stands on Discord."""

    kind: str            # "approval" or "question"
    request: dict
    workspace_name: str
    message_id: str = ""
    # question index -> picked option positions, plus render.text_key(index) ->
    # what was typed into the modal. Held here, not on Discord, because a
    # select interaction reports only its own values.
    state: dict = None

    def __post_init__(self):
        if self.state is None:
            self.state = {}


class DiscordBridge:
    """A `RemoteNotifier` that posts to a channel and answers from it."""

    # Interaction types and callback types, from Discord's docs.
    MESSAGE_COMPONENT = 3
    MODAL_SUBMIT = 5
    CHANNEL_MESSAGE = 4
    DEFERRED_UPDATE = 6
    UPDATE_MESSAGE = 7
    MODAL = 9
    EPHEMERAL = 1 << 6

    def __init__(self, *, config: DiscordConfig, api, registry):
        self.config = config
        self.api = api
        self.registry = registry
        self._posted: dict[str, _Posted] = {}

    def posted_requests(self) -> list[str]:
        """Request ids currently showing on Discord. Used by tests, and by the
        client's reconnect logging."""
        return list(self._posted)

    # --- RemoteNotifier -------------------------------------------------------

    async def approval_posted(self, request_id: str, session, request: dict) -> None:
        workspace_name = getattr(session, "workspace_name", "") or ""
        posted = _Posted(kind="approval", request=request, workspace_name=workspace_name)
        self._posted[request_id] = posted
        posted.message_id = await self.api.create_message(
            self.config.channel_id,
            render.approval_message(request, workspace_name=workspace_name),
        )

    async def question_posted(self, request_id: str, session, request: dict) -> None:
        workspace_name = getattr(session, "workspace_name", "") or ""
        posted = _Posted(kind="question", request=request, workspace_name=workspace_name)
        self._posted[request_id] = posted
        posted.message_id = await self.api.create_message(
            self.config.channel_id,
            render.question_message(request, workspace_name=workspace_name, state={}),
        )

    async def request_closed(self, request_id: str, session, outcome: str, by: str) -> None:
        """However it ended, the buttons go away.

        A phone left showing `허용` for a call that already ran is how the same
        command gets approved twice."""
        posted = self._posted.pop(request_id, None)
        if posted is None or not posted.message_id:
            return
        await self.api.edit_message(
            self.config.channel_id,
            posted.message_id,
            render.closed_message(
                posted.request,
                workspace_name=posted.workspace_name,
                outcome=outcome,
                by=by,
                kind=posted.kind,
            ),
        )

    async def turn_ended(self, session, text: str) -> None:
        if not self.config.turn_summaries:
            return
        await self.api.create_message(
            self.config.channel_id,
            render.turn_message(getattr(session, "workspace_name", "") or "", text),
        )

    # --- interactions ---------------------------------------------------------

    async def handle_interaction(self, payload: dict) -> None:
        """One click, one modal submit. Never raises: the gateway loop must
        survive a message it does not understand."""
        try:
            await self._handle(payload)
        except Exception:  # noqa: BLE001 - one bad interaction must not stop the bot
            logger.warning("discord interaction failed", exc_info=True)

    async def _handle(self, payload: dict) -> None:
        data = payload.get("data") or {}
        parsed = render.parse_custom_id(data.get("custom_id") or "")
        if parsed is None:
            # Another application's component in the same channel.
            return
        kind, request_id, field = parsed

        if str(payload.get("channel_id") or "") != self.config.channel_id:
            # Somebody forwarded the message elsewhere. Silence, not a refusal:
            # answering would confirm the request exists.
            return

        user_id = self._user_id(payload)
        if not self.config.may_answer(user_id):
            await self._ephemeral(payload, "이 요청에 답할 권한이 없다.")
            return

        by = f"discord:{user_id}"
        if kind == "approve":
            await self._approve(payload, request_id, field, by)
        elif kind == "pick":
            await self._pick(payload, request_id, field, data.get("values") or [])
        elif kind == "ask" and field == "text":
            await self._open_modal(payload, request_id)
        elif kind == "ask" and field == "submit":
            await self._submit(payload, request_id, by)
        elif kind == "modal":
            await self._modal_submitted(payload, request_id, data)

    def _user_id(self, payload: dict) -> str:
        member = payload.get("member") or {}
        user = member.get("user") or payload.get("user") or {}
        return str(user.get("id") or "")

    async def _respond(self, payload: dict, body: dict) -> None:
        await self.api.respond(payload.get("id") or "", payload.get("token") or "", body)

    async def _ephemeral(self, payload: dict, text: str) -> None:
        """Seen only by whoever clicked, so a refusal or a validation error does
        not fill the channel."""
        await self._respond(
            payload,
            {"type": self.CHANNEL_MESSAGE, "data": {"content": text, "flags": self.EPHEMERAL}},
        )

    async def _approve(self, payload: dict, request_id: str, field: str, by: str) -> None:
        decision = "allow" if field == "allow" else "deny"
        if not self.registry.resolve_remote_approval(request_id, decision, by):
            # Answered in the browser, or timed out, while this card was open.
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        # The message itself is rewritten by request_closed, which runs for
        # whichever side answered; here we only acknowledge the click.
        await self._respond(payload, {"type": self.DEFERRED_UPDATE})

    async def _pick(self, payload: dict, request_id: str, field: str, values: list) -> None:
        posted = self._posted.get(request_id)
        if posted is None:
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        if field.isdigit():
            posted.state[int(field)] = [str(value) for value in values]
        await self._respond(
            payload,
            {
                "type": self.UPDATE_MESSAGE,
                "data": render.question_message(
                    posted.request,
                    workspace_name=posted.workspace_name,
                    state=posted.state,
                ),
            },
        )

    async def _open_modal(self, payload: dict, request_id: str) -> None:
        posted = self._posted.get(request_id)
        if posted is None:
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        await self._respond(
            payload, {"type": self.MODAL, "data": render.modal_payload(posted.request)}
        )

    async def _modal_submitted(self, payload: dict, request_id: str, data: dict) -> None:
        posted = self._posted.get(request_id)
        if posted is None:
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        for row in data.get("components") or []:
            for component in row.get("components") or []:
                parsed = render.parse_custom_id(component.get("custom_id") or "")
                if parsed is None:
                    continue
                _, _, index = parsed
                if index.isdigit():
                    posted.state[render.text_key(int(index))] = [component.get("value") or ""]
        await self._respond(
            payload,
            {
                "type": self.UPDATE_MESSAGE,
                "data": render.question_message(
                    posted.request,
                    workspace_name=posted.workspace_name,
                    state=posted.state,
                ),
            },
        )

    async def _submit(self, payload: dict, request_id: str, by: str) -> None:
        posted = self._posted.get(request_id)
        if posted is None:
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        questions = list(posted.request.get("questions") or [])
        missing = render.missing_required(questions, posted.state)
        if missing:
            names = ", ".join(
                (questions[index].get("question") or f"질문 {index + 1}") for index in missing
            )
            await self._ephemeral(payload, f"아직 답하지 않은 질문이 있다: {names}")
            return
        answers = render.collect_answers(questions, posted.state)
        if not self.registry.resolve_remote_answer(request_id, answers, by):
            await self._ephemeral(payload, "이미 처리된 요청이다.")
            return
        await self._respond(payload, {"type": self.DEFERRED_UPDATE})
