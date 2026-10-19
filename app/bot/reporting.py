from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.types import FSInputFile
from sqlalchemy import func, or_, select

from app.bot.common import (
    PAGE,
    Input,
    date,
    hide_phones,
    home_row,
    kb,
    owner,
    pages,
    show,
    still_authorized,
)
from app.enums import JobStatus, ResultStatus
from app.models import AccountJobResult, Job, JobChannel, utcnow
from app.services.access import allowed_job, channel_scope
from app.services.control import audit

STATUS = {
    "pending": "в очереди",
    "running": "в работе",
    "completed": "завершено",
    "partial": "частично",
    "failed": "ошибка",
    "cancelled": "остановлено",
}


def create_router(s):
    router = Router(name="reporting")

    async def listing(event, state, kind="all", page=0):
        data = await state.get_data()
        search = data.get("report_search", "")
        query = select(Job)
        scope = await channel_scope(s.settings, s.db, event.from_user.id)
        if scope is not None:
            query = query.where(
                Job.id.not_in(select(JobChannel.job_id).where(JobChannel.channel_id.not_in(scope)))
            )
        if kind == "active":
            query = query.where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        elif kind == "failed":
            query = query.where(
                Job.status.in_([JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.CANCELLED])
            )
        elif kind == "due":
            query = query.where(
                Job.finished_at.is_not(None),
                Job.retention_until <= utcnow() + timedelta(days=s.settings.report_reminder_days),
                Job.deletion_approved.is_(False),
            )
        if search:
            ids = select(AccountJobResult.job_id).where(
                or_(
                    AccountJobResult.channel_title.icontains(search, autoescape=True),
                    AccountJobResult.channel_url.icontains(search, autoescape=True),
                )
            )
            query = query.where(or_(Job.id.contains(search, autoescape=True), Job.id.in_(ids)))
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(Job.created_at.desc())
                        .offset(max(0, page) * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            event,
            f"<b>Отчёты</b> · {page + 1}" + (f"\nПоиск: {escape(search)}" if search else ""),
            kb(
                *[
                    [
                        (
                            f"{r.id[:8]} · {STATUS[r.status.value]} · {date(s, r.created_at)}",
                            f"job:{r.id}",
                            30,
                        )
                    ]
                    for r in rows[:PAGE]
                ],
                [
                    ("Все", "reports:all:0"),
                    ("В работе", "reports:active:0"),
                    ("С проблемами", "reports:failed:0"),
                ],
                [
                    ("Срок хранения", "reports:due:0"),
                    ("Поиск", "report:search", 123),
                    ("Сброс", "report:reset"),
                ],
                [
                    ("Сводка за день", "report:daily", 21),
                    ("Ошибки · XLSX", "report:problems", 30),
                ],
                pages(f"reports:{kind}", page, len(rows) > PAGE),
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("reports:"))
    async def report_list(c, state):
        _, kind, page = c.data.split(":")
        await listing(c, state, kind, int(page))

    @router.callback_query(F.data == "report:reset")
    async def reset(c, state):
        await state.update_data(report_search="")
        await listing(c, state)

    @router.callback_query(F.data.in_({"report:search", "report:daily"}))
    async def search(c, state):
        await state.set_state(Input.text)
        await state.update_data(
            action="report_search" if c.data == "report:search" else "daily_report"
        )
        await show(
            c,
            "Введите часть ID задания, название или ссылку канала."
            if c.data == "report:search"
            else f"Введите дату ГГГГ-ММ-ДД или «сегодня». Часовой пояс: {s.settings.timezone}.",
            kb(home_row()),
        )

    async def card(event, job_id):
        if not await allowed_job(s.settings, s.db, event.from_user.id, job_id):
            raise ValueError("Нет доступа к каналам этого задания")
        async with s.db.session() as session:
            job = await session.get(Job, job_id)
            if not job:
                raise ValueError("Отчёт не найден или уже удалён после истечения срока хранения")
            grouped = (
                await session.execute(
                    select(AccountJobResult.state, AccountJobResult.status, func.count())
                    .where(AccountJobResult.job_id == job_id)
                    .group_by(AccountJobResult.state, AccountJobResult.status)
                )
            ).all()
            views = await session.scalar(
                select(func.coalesce(func.sum(AccountJobResult.viewed_posts), 0)).where(
                    AccountJobResult.job_id == job_id
                )
            )
            problems = list(
                (
                    await session.scalars(
                        select(AccountJobResult)
                        .where(
                            AccountJobResult.job_id == job_id, AccountJobResult.error.is_not(None)
                        )
                        .limit(3)
                    )
                ).all()
            )
        recorded = sum(count for _, _, count in grouped)
        done = sum(count for stage, _, count in grouped if stage == "done")
        success = sum(
            count
            for stage, status, count in grouped
            if stage == "done" and status == ResultStatus.COMPLETED
        )
        text = (
            f"<b>Задание {job.id[:8]}</b> · {STATUS[job.status.value]}\n"
            f"Начало: {date(s, job.started_at or job.created_at)}\nКонец: {date(s, job.finished_at or job.ends_at)}\n"
            f"Выполнено: {done}/{job.expected_results} · успешно: {success}\n"
            f"Запросов просмотра: {views} · записей: {recorded}\n"
            f"Хранить до: {date(s, job.retention_until)}\n"
            + (
                "Удаление подтверждено на дату окончания хранения.\n"
                if job.deletion_approved
                else ""
            )
            + "\n".join(f"№{r.account_number}: {escape((r.error or '')[:180])}" for r in problems)
        )
        if (job.summary or {}).get("legacy_planning_unknown"):
            text += "\nДанные версии 1.2: полный план прошлого запуска неизвестен; показаны сохранившиеся результаты."
        rows = [
            [("Обновить", f"job:{job_id}", 63), ("Скачать XLSX", f"report:export:{job_id}", 30)]
        ]
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING) and (
            owner(s, event.from_user.id) or job.created_by == event.from_user.id
        ):
            rows.append([("Остановить задание", f"report:stop:{job_id}", 125)])
        if job.finished_at and owner(s, event.from_user.id):
            rows.append(
                [
                    ("Хранить ещё 30 дней", f"own:report:keep:{job_id}", 37),
                    ("Подтвердить удаление", f"own:report:delete:{job_id}", 33),
                ]
            )
        await show(event, text, kb(*rows, [("К отчётам", "reports:all:0", 67)], home_row()))

    @router.callback_query(F.data.startswith("job:"))
    async def job_card(c):
        await card(c, c.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("report:stop:"))
    async def stop(c):
        job_id = c.data.split(":", 2)[2]
        if not await allowed_job(s.settings, s.db, c.from_user.id, job_id):
            raise ValueError("Нет доступа к каналам этого задания")
        async with s.db.session() as session:
            job = await session.get(Job, job_id)
        if not job or (not owner(s, c.from_user.id) and job.created_by != c.from_user.id):
            raise ValueError("Остановить задание может его автор или главный администратор")
        await s.runner.stop_job(job_id, c.from_user.id)
        await c.message.answer(
            "Остановка сохранена. Текущий запрос завершается, после него работа прекратится."
        )
        await card(c, job_id)

    @router.callback_query(F.data.startswith("report:export:"))
    async def export(c):
        job_id = c.data.split(":", 2)[2]
        if not await allowed_job(s.settings, s.db, c.from_user.id, job_id):
            raise ValueError("Нет доступа к каналам этого задания")
        mask = await hide_phones(s, c.from_user.id)

        async def build():
            path = await s.reports.build_job_report(job_id, mask_phones=mask)
            if not await allowed_job(s.settings, s.db, c.from_user.id, job_id):
                return
            if not await still_authorized(s, c.from_user.id):
                return
            if not mask and await hide_phones(s, c.from_user.id):
                path = await s.reports.build_job_report(job_id, mask_phones=True)
            if not await still_authorized(s, c.from_user.id) or not await allowed_job(
                s.settings, s.db, c.from_user.id, job_id
            ):
                return
            await c.message.answer_document(FSInputFile(path), protect_content=True)
            await audit(s.db, c.from_user.id, "report.exported", job_id, masked=mask)

        s.spawn(build(), c.message)
        await c.message.answer("Готовлю XLSX. Он появится в этом чате.")

    @router.callback_query(F.data == "report:problems")
    async def problems(c):
        mask = await hide_phones(s, c.from_user.id)

        async def build():
            path = await s.reports.build_problem_accounts_report(mask_phones=mask)
            if not await still_authorized(s, c.from_user.id):
                return
            if not mask and await hide_phones(s, c.from_user.id):
                path = await s.reports.build_problem_accounts_report(mask_phones=True)
            await c.message.answer_document(FSInputFile(path), protect_content=True)
            await audit(s.db, c.from_user.id, "accounts.exported", masked=mask)

        s.spawn(build(), c.message)

    @router.callback_query(F.data.startswith("own:report:"))
    async def retention(c):
        _, _, action, id_ = c.data.split(":")
        if action == "keep":
            await s.retention.keep(id_, c.from_user.id)
            await card(c, id_)
        elif action == "delete":
            await show(
                c,
                "Подтвердить удаление отчёта и его результатов после истечения срока хранения? Пока срок не наступил, файл останется доступен.",
                kb(
                    [("Подтверждаю", f"own:report:confirm:{id_}", 33)],
                    [("Отмена", f"job:{id_}", 67)],
                ),
            )
        elif action == "confirm":
            await s.retention.delete_confirmed(id_, c.from_user.id)
            await show(
                c,
                "Подтверждение сохранено. Отчёт удаляется только по окончании срока хранения.",
                kb([("Отчёты", "reports:all:0")]),
            )

    async def text_input(message, state, data):
        if data.get("action") == "report_search":
            await state.update_data(report_search=message.text.strip()[:64])
            await state.set_state(None)
            await listing(message, state)
        elif data.get("action") == "daily_report":
            value = message.text.strip()
            day = (
                utcnow().astimezone(s.settings.tz).date()
                if value.lower() == "сегодня"
                else datetime.strptime(value, "%Y-%m-%d").date()
            )
            await state.clear()

            async def build():
                scope = await channel_scope(s.settings, s.db, message.from_user.id)
                path = await s.reports.daily(day, channel_ids=scope)
                current_scope = await channel_scope(s.settings, s.db, message.from_user.id)
                if current_scope != scope:
                    path = await s.reports.daily(day, channel_ids=current_scope)
                if not await still_authorized(s, message.from_user.id):
                    return
                await message.answer_document(FSInputFile(path), protect_content=True)
                await audit(s.db, message.from_user.id, "report.daily_exported", day.isoformat())

            s.spawn(build(), message)
            await message.answer(
                "Готовлю сводку дня. Она включает задания, созданные в эту дату.",
                reply_markup=kb(home_row()),
            )
        else:
            return False
        return True

    return router, text_input
