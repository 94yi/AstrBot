import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot import logger
from astrbot.core.agent.runners.base import AgentState
from astrbot.core.agent.tool import ToolSet
from astrbot.core.config.agent_runner import resolve_context_compression_config
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.db import BaseDatabase
from astrbot.core.db.po import CronJob
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entites import ProviderRequest
from astrbot.core.utils.config_number import coerce_int_config
from astrbot.core.utils.history_saver import persist_agent_history

if TYPE_CHECKING:
    from astrbot.core.star.context import Context


_CRONTAB_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_CRONTAB_WEEKDAY_PATTERN = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")
_CHINESE_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _holiday_period_for_date(
    policy: dict[str, Any] | None, target_date: date
) -> dict[str, Any] | None:
    """Return the configured holiday period containing one local date.

    Args:
        policy: Course holiday policy stored in the cron payload
        target_date: Local calendar date to check

    Returns:
        The matching normalized period, or ``None`` when classes are allowed

    Raises:
        ValueError: If the persisted holiday policy is malformed
    """
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise ValueError("course_holiday_policy must be an object")
    raw_periods = policy.get("periods", [])
    raw_class_dates = policy.get("class_dates", [])
    if not isinstance(raw_periods, list) or len(raw_periods) > 64:
        raise ValueError("invalid course holiday periods")
    if not isinstance(raw_class_dates, list) or len(raw_class_dates) > 128:
        raise ValueError("invalid course holiday class dates")
    try:
        class_dates = {date.fromisoformat(str(value)) for value in raw_class_dates}
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid course holiday class date") from exc
    if target_date in class_dates:
        return None
    for raw_period in raw_periods:
        if not isinstance(raw_period, dict):
            raise ValueError("invalid course holiday period")
        try:
            start = date.fromisoformat(str(raw_period["start"]))
            end = date.fromisoformat(str(raw_period["end"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid course holiday period dates") from exc
        name = str(raw_period.get("name") or "假期").strip()
        if end < start or not name or len(name) > 80 or "\n" in name:
            raise ValueError("invalid course holiday period values")
        if start <= target_date <= end:
            return {"name": name, "start": start, "end": end}
    return None


def _render_structured_daily_schedule(
    schedule: dict[str, Any],
    run_started_at: datetime,
    timezone_name: str,
    holiday_policy: dict[str, Any] | None = None,
) -> str | None:
    """Render a bounded daily schedule without involving an LLM or chat history.

    Args:
        schedule: Validated weekly course schedule
        run_started_at: UTC or timezone-aware scheduler execution time
        timezone_name: IANA timezone used to select the local day
        holiday_policy: Optional holiday periods and confirmed class-date overrides

    Returns:
        Direct-send course text, or ``None`` when no reminder should be sent

    Raises:
        ValueError: If schedule or holiday metadata is malformed
    """
    if not isinstance(schedule, dict):
        raise ValueError("structured_daily_schedule must be an object")
    try:
        tzinfo = ZoneInfo(timezone_name)
        anchor = date.fromisoformat(str(schedule["anchor_monday"]))
        anchor_week = int(schedule["anchor_week"])
        first_week = int(schedule["active_week_start"])
        last_week = int(schedule["active_week_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid structured daily schedule metadata") from exc
    if anchor.weekday() != 0 or not 1 <= anchor_week <= first_week <= last_week <= 60:
        raise ValueError("invalid structured daily schedule week range")
    current = run_started_at
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_date = current.astimezone(tzinfo).date()
    if _holiday_period_for_date(holiday_policy, local_date):
        return None
    local_monday = local_date - timedelta(days=local_date.weekday())
    week = anchor_week + (local_monday - anchor).days // 7
    if not first_week <= week <= last_week:
        return None
    weekdays = schedule.get("weekdays")
    if not isinstance(weekdays, dict):
        raise ValueError("structured daily schedule weekdays must be an object")
    raw_lessons = weekdays.get(str(local_date.weekday()), [])
    if not isinstance(raw_lessons, list) or len(raw_lessons) > 20:
        raise ValueError("invalid structured daily schedule lessons")
    lessons = []
    for raw in raw_lessons:
        if not isinstance(raw, dict):
            raise ValueError("invalid structured daily schedule lesson")
        values = tuple(
            str(raw.get(key) or "").strip() for key in ("time", "course", "location")
        )
        if any(not value or len(value) > 120 or "\n" in value for value in values):
            raise ValueError("invalid structured daily schedule lesson fields")
        lessons.append("｜".join(values))
    heading = f"今天是第 {week} 周{_CHINESE_WEEKDAYS[local_date.weekday()]}，课程提醒："
    if lessons:
        parts = [heading, *lessons]
    else:
        parts = [
            f"今天是第 {week} 周{_CHINESE_WEEKDAYS[local_date.weekday()]}，今天没有常规课。"
        ]
    notice = str(schedule.get("notice") or "").strip()
    closing = str(schedule.get("closing") or "").strip()
    tomorrow = local_date + timedelta(days=1)
    tomorrow_holiday = _holiday_period_for_date(holiday_policy, tomorrow)
    confirmation = ""
    if tomorrow_holiday and tomorrow_holiday["start"] == tomorrow:
        confirmation = (
            f"明天起是{tomorrow_holiday['name']}假期，常规课程提醒会默认暂停，"
            "有调课或补课吗？把课程、日期、时间和地点告诉我就好"
        )
    for value in (notice, confirmation, closing):
        if len(value) > 500:
            raise ValueError("structured daily schedule text is too long")
        if value:
            parts.append(value)
    return "\n\n".join(
        ["\n".join(parts[: 1 + len(lessons)]), *parts[1 + len(lessons) :]]
    )


def _normalize_crontab_day_of_week(day_of_week: str) -> str:
    """Normalize standard crontab weekdays for APScheduler.

    APScheduler treats numeric weekdays as Monday=0, while standard crontab and
    AstrBot's WebUI use Sunday=0/7. Numeric weekday fields are expanded to
    weekday names so the scheduled day remains unambiguous.

    Args:
        day_of_week: The day-of-week field from a five-part crontab expression.

    Returns:
        A day-of-week field compatible with APScheduler.

    Raises:
        ValueError: If a numeric weekday value or step is outside the supported
            crontab range.
    """
    normalized_parts: list[str] = []
    for raw_part in day_of_week.split(","):
        part = raw_part.strip().lower()
        match = _CRONTAB_WEEKDAY_PATTERN.fullmatch(part)
        if not match:
            normalized_parts.append(part)
            continue

        wildcard, start_text, end_text, step_text = match.groups()
        step = int(step_text or "1")
        if step < 1:
            raise ValueError("day_of_week step must be greater than 0")

        if wildcard:
            if step == 1:
                normalized_parts.append("*")
                continue
            values = range(0, 7, step)
        else:
            start = int(start_text)
            end = int(end_text) if end_text is not None else None
            if start < 0 or start > 7 or (end is not None and (end < 0 or end > 7)):
                raise ValueError("day_of_week values must be between 0 and 7")
            if end is not None and start > end:
                raise ValueError("day_of_week range start must not exceed end")
            if end is None:
                end = 7 if step_text else start
            values = range(start, end + 1, step)

        weekdays: list[int] = []
        for value in values:
            weekday = 0 if value == 7 else value
            if weekday not in weekdays:
                weekdays.append(weekday)

        if len(weekdays) == 7:
            normalized_parts.append("*")
        else:
            normalized_parts.extend(_CRONTAB_WEEKDAY_NAMES[value] for value in weekdays)

    return ",".join(normalized_parts)


class CronJobSchedulingError(Exception):
    """Raised when a cron job fails to be scheduled."""

    pass


class CronJobManager:
    """Central scheduler for BasicCronJob and ActiveAgentCronJob."""

    def __init__(self, db: BaseDatabase) -> None:
        self.db = db
        self.scheduler = AsyncIOScheduler()
        self._basic_handlers: dict[str, Callable[..., Any]] = {}
        self._lock = asyncio.Lock()
        self._started = False
        # The scheduler may start early via _schedule_job; track DB sync separately.
        self._db_synced = False

    async def start(self, ctx: "Context") -> None:
        self.ctx: Context = ctx  # star context
        async with self._lock:
            if self._db_synced:
                return
            if not self._started:
                self.scheduler.start()
                self._started = True
            await self.sync_from_db()
            self._db_synced = True

    async def shutdown(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self.scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
            self._started = False
            self._db_synced = False

    async def sync_from_db(self) -> None:
        jobs = await self.db.list_cron_jobs()
        for job in jobs:
            if not job.enabled or not job.persistent:
                continue
            if job.job_type == "basic" and job.job_id not in self._basic_handlers:
                logger.warning(
                    "Skip scheduling basic cron job %s due to missing handler.",
                    job.job_id,
                )
                continue
            try:
                self._schedule_job(job)
            except CronJobSchedulingError:
                continue  # Error already logged in _schedule_job

    async def add_basic_job(
        self,
        *,
        name: str,
        cron_expression: str,
        handler: Callable[..., Any | Awaitable[Any]],
        description: str | None = None,
        timezone: str | None = None,
        payload: dict | None = None,
        enabled: bool = True,
        persistent: bool = False,
    ) -> CronJob:
        job = await self.db.create_cron_job(
            name=name,
            job_type="basic",
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload or {},
            description=description,
            enabled=enabled,
            persistent=persistent,
        )
        self._basic_handlers[job.job_id] = handler
        if enabled:
            self._schedule_job(job)
        return job

    async def add_active_job(
        self,
        *,
        name: str,
        cron_expression: str | None,
        payload: dict,
        description: str | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        persistent: bool = True,
        run_once: bool = False,
        run_at: datetime | None = None,
    ) -> CronJob:
        # If run_once with run_at, store run_at in payload for later reference.
        if run_once and run_at:
            payload = {**payload, "run_at": run_at.isoformat()}
        job = await self.db.create_cron_job(
            name=name,
            job_type="active_agent",
            cron_expression=cron_expression,
            timezone=timezone,
            payload=payload,
            description=description,
            enabled=enabled,
            persistent=persistent,
            run_once=run_once,
        )
        if enabled:
            self._schedule_job(job)
        return job

    async def update_job(self, job_id: str, **kwargs) -> CronJob | None:
        current_job = await self.db.get_cron_job(job_id)
        if not current_job:
            return None
        candidate = current_job.model_copy(update=kwargs)
        if candidate.enabled:
            # Invalid edits must not overwrite the durable job or remove its
            # working schedule. Disabled legacy jobs can still be corrected.
            self._build_trigger(candidate)
        job = await self.db.update_cron_job(job_id, **kwargs)
        if not job:
            return None
        self._remove_scheduled(job_id)
        if job.enabled:
            self._schedule_job(job)
        return job

    async def delete_job(self, job_id: str) -> None:
        self._remove_scheduled(job_id)
        self._basic_handlers.pop(job_id, None)
        await self.db.delete_cron_job(job_id)

    async def list_jobs(self, job_type: str | None = None) -> list[CronJob]:
        return await self.db.list_cron_jobs(job_type)

    def _remove_scheduled(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def _build_trigger(self, job: CronJob) -> CronTrigger | DateTrigger:
        """Validate a job's timing without modifying stored or scheduled jobs.

        Args:
            job: Candidate job definition, including one-shot payload fields.

        Returns:
            A trigger using the same timezone and weekday rules as scheduling.

        Raises:
            CronJobSchedulingError: If the schedule cannot be parsed.
        """
        try:
            tzinfo = None
            if job.timezone:
                try:
                    tzinfo = ZoneInfo(job.timezone)
                except Exception:
                    logger.warning(
                        "Invalid timezone %s for cron job %s, fallback to system.",
                        job.timezone,
                        job.job_id,
                    )
            if job.run_once:
                run_at_str = None
                if isinstance(job.payload, dict):
                    run_at_str = job.payload.get("run_at")
                run_at_str = run_at_str or job.cron_expression
                if not run_at_str:
                    raise ValueError("run_once job missing run_at timestamp")
                run_at = datetime.fromisoformat(run_at_str)
                if run_at.tzinfo is None and tzinfo is not None:
                    run_at = run_at.replace(tzinfo=tzinfo)
                trigger = DateTrigger(run_date=run_at, timezone=tzinfo)
            else:
                if not job.cron_expression:
                    raise ValueError("recurring job missing cron_expression")
                minute, hour, day, month, day_of_week = job.cron_expression.split()
                normalized_cron_expression = " ".join(
                    [
                        minute,
                        hour,
                        day,
                        month,
                        _normalize_crontab_day_of_week(day_of_week),
                    ]
                )
                trigger = CronTrigger.from_crontab(
                    normalized_cron_expression, timezone=tzinfo
                )
            return trigger
        except (ValueError, TypeError) as e:
            logger.exception("Failed to build trigger for cron job %s", job.job_id)
            raise CronJobSchedulingError(str(e)) from e

    def _schedule_job(self, job: CronJob) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True
        try:
            trigger = self._build_trigger(job)
            self.scheduler.add_job(
                self._run_job,
                id=job.job_id,
                trigger=trigger,
                args=[job.job_id],
                replace_existing=True,
                misfire_grace_time=30,
            )
            asyncio.create_task(
                self.db.update_cron_job(
                    job.job_id, next_run_time=self._get_next_run_time(job.job_id)
                )
            )
        except (ValueError, TypeError) as e:
            logger.exception("Failed to schedule cron job %s", job.job_id)
            raise CronJobSchedulingError(str(e)) from e

    def _get_next_run_time(self, job_id: str):
        aps_job = self.scheduler.get_job(job_id)
        if not aps_job or aps_job.next_run_time is None:
            return None
        return aps_job.next_run_time.astimezone(timezone.utc)

    def get_next_run_time(self, job_id: str) -> datetime | None:
        """Read the live next-run time straight from the scheduler.

        The DB copy of ``next_run_time`` is written via a fire-and-forget
        task in ``_schedule_job``, so it can still be stale/None right after
        ``add_active_job``/``update_job`` return. The scheduler itself is
        updated synchronously, so callers that need an immediate answer
        should use this instead of the job row's ``next_run_time`` field.

        Args:
            job_id: The scheduled job's ID.

        Returns:
            The job's next scheduled run time in UTC, or None if the job
            is not currently scheduled.
        """
        return self._get_next_run_time(job_id)

    async def run_job_now(self, job_id: str) -> None:
        await self._run_job(job_id, ignore_enabled=True, delete_run_once=False)

    async def _run_job(
        self,
        job_id: str,
        *,
        ignore_enabled: bool = False,
        delete_run_once: bool = True,
    ) -> None:
        job = await self.db.get_cron_job(job_id)
        if not job or (not job.enabled and not ignore_enabled):
            return
        start_time = datetime.now(timezone.utc)
        await self.db.update_cron_job(
            job_id, status="running", last_run_at=start_time, last_error=None
        )
        status = "completed"
        last_error = None
        try:
            if job.job_type == "basic":
                await self._run_basic_job(job)
            elif job.job_type == "active_agent":
                await self._run_active_agent_job(job, start_time=start_time)
            else:
                raise ValueError(f"Unknown cron job type: {job.job_type}")
        except Exception as e:  # noqa: BLE001
            status = "failed"
            last_error = str(e)
            logger.error(f"Cron job {job_id} failed: {e!s}", exc_info=True)
        finally:
            next_run = self._get_next_run_time(job_id)
            update_fields = {
                "status": status,
                "last_run_at": start_time,
                "last_error": last_error,
                "next_run_time": next_run,
            }
            if job.run_once and status == "failed":
                update_fields["enabled"] = False
            await self.db.update_cron_job(job_id, **update_fields)
            if job.run_once and delete_run_once and status == "completed":
                # Keep failed one-shot jobs for explicit inspection/retry.
                # Automatic retries are unsafe when a transport timeout may
                # mean that the remote platform accepted the message.
                await self.delete_job(job_id)

    async def _run_basic_job(self, job: CronJob) -> None:
        handler = self._basic_handlers.get(job.job_id)
        if not handler:
            raise RuntimeError(f"Basic cron job handler not found for {job.job_id}")
        payload = job.payload or {}
        result = handler(**payload) if payload else handler()
        if asyncio.iscoroutine(result):
            await result

    async def _run_active_agent_job(self, job: CronJob, start_time: datetime) -> None:
        payload = job.payload or {}
        delivery_session_str = str(payload.get("session") or "").strip()
        holiday_policy = payload.get("course_holiday_policy")
        if holiday_policy is not None:
            timezone_name = job.timezone or str(holiday_policy.get("timezone") or "UTC")
            try:
                tzinfo = ZoneInfo(timezone_name)
            except Exception as exc:
                raise ValueError("invalid course holiday timezone") from exc
            current = start_time
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            local_date = current.astimezone(tzinfo).date()
            holiday = _holiday_period_for_date(holiday_policy, local_date)
            if holiday:
                logger.info(
                    "Course reminder %s skipped for %s holiday %s",
                    job.job_id,
                    holiday["name"],
                    local_date.isoformat(),
                )
                return
        structured_schedule = payload.get("structured_daily_schedule")
        if structured_schedule is not None:
            if not delivery_session_str:
                raise ValueError("structured schedule requires a delivery session")
            content = _render_structured_daily_schedule(
                structured_schedule,
                start_time,
                job.timezone or str(structured_schedule.get("timezone") or "UTC"),
                holiday_policy,
            )
            if content is None:
                logger.info(
                    "Structured schedule %s is outside its active weeks", job.job_id
                )
                return
            sent = await self.ctx.send_message(
                delivery_session_str, MessageChain().message(content)
            )
            if not sent:
                raise RuntimeError("Structured schedule delivery failed")
            logger.info(
                "Structured schedule %s delivered for %s",
                job.job_id,
                content.split("，", 1)[0],
            )
            return
        session_str = delivery_session_str or str(
            MessageSession(
                platform_name="cron",
                message_type=MessageType.OTHER_MESSAGE,
                session_id=job.job_id,
            )
        )
        note = payload.get("note") or job.description or job.name

        extras = {
            "cron_job": {
                "id": job.job_id,
                "name": job.name,
                "type": job.job_type,
                "run_once": job.run_once,
                "description": job.description,
                "note": note,
                "run_started_at": start_time.isoformat(),
                "run_at": (
                    job.payload.get("run_at") if isinstance(job.payload, dict) else None
                ),
                "session": delivery_session_str,
            },
            "cron_payload": payload,
        }

        await self._woke_main_agent(
            message=note,
            session_str=session_str,
            extras=extras,
            delivery_session_str=delivery_session_str,
        )

    async def _woke_main_agent(
        self,
        *,
        message: str,
        session_str: str,
        extras: dict,
        delivery_session_str: str = "",
    ) -> None:
        """Woke the main agent to handle the cron job message."""
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            _get_session_conv,
            build_main_agent,
        )
        from astrbot.core.astr_main_agent_resources import (
            PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT,
        )
        from astrbot.core.tools.message_tools import SendMessageToUserTool

        try:
            session = (
                session_str
                if isinstance(session_str, MessageSession)
                else MessageSession.from_str(session_str)
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Invalid session for cron job: {e}")
            return

        cron_event = CronMessageEvent(
            context=self.ctx,
            session=session,
            message=message,
            extras=extras or {},
            message_type=session.message_type,
        )

        # judge user's role
        umo = cron_event.unified_msg_origin
        cfg = self.ctx.get_config(umo=umo)
        enabled_plugins_name = cfg.get("plugin_set", ["*"])
        cron_event.plugins_name = (
            None if enabled_plugins_name == ["*"] else enabled_plugins_name
        )
        cron_payload = extras.get("cron_payload", {}) if extras else {}
        sender_id = cron_payload.get("sender_id")
        admin_ids = cfg.get("admins_id", [])
        if admin_ids:
            cron_event.role = "admin" if sender_id in admin_ids else "member"
        if cron_payload.get("origin", "tool") == "api":
            cron_event.role = "admin"

        provider_settings = cfg.get("provider_settings", {}) or {}
        persona_config = (
            cfg.get("agent_runner", {}).get("config", {}).get("persona", {})
        )
        tool_call_timeout = (
            cfg.get("agent_runner", {})
            .get("config", {})
            .get("misc", {})
            .get("tool_call_timeout", 120)
        )
        agent_max_step = coerce_int_config(
            cfg.get("agent_runner", {})
            .get("config", {})
            .get("misc", {})
            .get("max_steps", 128),
            default=128,
            min_value=1,
            field_name="agent_runner.config.misc.max_steps",
        )
        config = MainAgentBuildConfig(
            tool_call_timeout=tool_call_timeout,
            fallback_provider_ids=cfg.get("agent_runner", {})
            .get("config", {})
            .get("model", {})
            .get("fallback_provider_ids", []),
            **resolve_context_compression_config(
                cfg.get("agent_runner", {}).get("config", {}).get("compression", {})
            ),
            llm_safety_mode=persona_config.get("safety_mode", True),
            safety_mode_strategy=persona_config.get(
                "safety_mode_strategy", "system_prompt"
            ),
            streaming_response=False,
            computer_use_runtime=provider_settings.get("computer_use_runtime", "none"),
            sandbox_cfg=provider_settings.get("sandbox", {}),
            provider_settings=provider_settings,
        )
        req = ProviderRequest()
        conv = await _get_session_conv(event=cron_event, plugin_context=self.ctx)
        req.conversation = conv
        req.contexts = json.loads(conv.history)
        cron_job_str = json.dumps(extras.get("cron_job", {}), ensure_ascii=False)
        req.system_prompt += PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT.format(
            cron_job=cron_job_str
        )
        req.prompt = (
            "You are now responding to a scheduled task. "
            "Proceed according to your system instructions. "
            "Output using same language as previous conversation. "
            "After completing your task, summarize and output your actions and results."
        )
        if delivery_session_str:
            if not req.func_tool:
                req.func_tool = ToolSet()
            req.func_tool.add_tool(
                self.ctx.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
            )

        result = await build_main_agent(
            event=cron_event, plugin_context=self.ctx, config=config, req=req
        )
        if not result:
            raise RuntimeError("Failed to build main agent for cron job.")

        runner = result.agent_runner
        async for _ in runner.step_until_done(agent_max_step):
            # agent will send message to user via using tools
            pass
        llm_resp = runner.get_final_llm_resp()
        if runner.state == AgentState.ERROR:
            # The run failed (e.g. malformed function call at max steps) but
            # no exception escapes the runner; without this the job was
            # recorded as completed with last_error=NULL and the user saw
            # only intermediate messages (#9980).
            detail = (
                f": {llm_resp.completion_text}"
                if llm_resp and llm_resp.completion_text
                else ""
            )
            raise RuntimeError(f"Cron agent run ended in ERROR state{detail}")
        cron_meta = extras.get("cron_job", {}) if extras else {}
        summary_note = (
            f"[CronJob] {cron_meta.get('name') or cron_meta.get('id', 'unknown')}: {cron_meta.get('description', '')} "
            f" triggered at {cron_meta.get('run_started_at', 'unknown time')}, "
        )
        if llm_resp and llm_resp.role == "assistant":
            summary_note += (
                f"I finished this job, here is the result: {llm_resp.completion_text}"
            )

        await persist_agent_history(
            self.ctx.conversation_manager,
            event=cron_event,
            req=req,
            summary_note=summary_note,
        )
        if delivery_session_str and not cron_event.get_extra(
            "_cron_delivery_skipped", False
        ):
            if not cron_event.get_extra(
                "_send_message_to_user_delivery_succeeded", False
            ):
                attempted = cron_event.get_extra(
                    "_send_message_to_user_attempted", False
                )
                reason = "failed" if attempted else "was not attempted"
                raise RuntimeError(
                    f"Scheduled message delivery {reason}; no success receipt."
                )
        if not llm_resp:
            logger.warning("Cron job agent got no response")
            return


__all__ = ["CronJobManager"]
