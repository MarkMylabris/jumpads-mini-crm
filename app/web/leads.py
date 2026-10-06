"""Страницы лидов: список с фильтрами, карточка, форма, заметки и теги (HTMX)."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Lead, LeadStatus, Tag
from app.services import leads as svc
from app.web.auth import require_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_user)])


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request"))


async def _load_lead(session: AsyncSession, lead_id: int, with_messages: bool = False) -> Lead:
    lead = await svc.get_lead(session, lead_id, with_messages=with_messages)
    if lead is None:
        raise HTTPException(404, "Лид не найден")
    return lead


def _int_list(values: list[str]) -> list[int]:
    return [int(v) for v in values if str(v).isdigit()]


@router.get("/")
async def index():
    return RedirectResponse("/leads", status_code=303)


@router.get("/leads")
async def leads_list(
    request: Request,
    q: str = "",
    source: str = "",
    status: str = "",
    tag: list[str] = Query(default_factory=list),
    tag_mode: str = "any",
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    f = svc.LeadFilter(q=q, source=source, status=status, tags=tag, tag_mode=tag_mode, page=page)
    result = await svc.list_leads(session, f)
    all_tags = await svc.list_tags(session)
    ctx = {
        "f": f,
        "page": result,
        "all_tags": all_tags,
        "qs": lambda **kw: urlencode(f.query_params(**kw)),
    }
    # Автообновлению и фильтрам через HTMX нужна только таблица, а не вся страница
    tpl = "partials/leads_table.html" if request.headers.get("HX-Target") == "leads-table" else "leads/list.html"
    return templates.TemplateResponse(request, tpl, ctx)


# --- создание и редактирование


def _form_ctx(lead: Lead | None, all_tags: list[Tag], form: dict | None = None, errors: dict | None = None) -> dict:
    if form is None:
        form = {
            "name": lead.name if lead else "",
            "contact": lead.contact if lead else "",
            "request": lead.request if lead else "",
            "status": lead.status.value if lead else LeadStatus.new.value,
            "tag_ids": [t.id for t in lead.tags] if lead else [],
        }
    return {"lead": lead, "all_tags": all_tags, "form": form, "errors": errors or {}}


async def _read_lead_form(request: Request) -> dict:
    data = await request.form()
    return {
        "name": str(data.get("name", "")),
        "contact": str(data.get("contact", "")),
        "request": str(data.get("request", "")),
        "status": str(data.get("status", LeadStatus.new.value)),
        "tag_ids": _int_list(data.getlist("tag_ids")),
    }


@router.get("/leads/new")
async def lead_new_form(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(request, "leads/form.html", _form_ctx(None, await svc.list_tags(session)))


@router.post("/leads/new")
async def lead_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _read_lead_form(request)
    try:
        lead = await svc.create_manual_lead(session, **form)
    except svc.ValidationError as e:
        ctx = _form_ctx(None, await svc.list_tags(session), form, e.errors)
        return templates.TemplateResponse(request, "leads/form.html", ctx, status_code=422)
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.get("/leads/{lead_id}")
async def lead_detail(request: Request, lead_id: int, session: AsyncSession = Depends(get_session)):
    lead = await _load_lead(session, lead_id, with_messages=True)
    return templates.TemplateResponse(
        request, "leads/detail.html", {"lead": lead, "all_tags": await svc.list_tags(session), "note_error": None}
    )


@router.get("/leads/{lead_id}/edit")
async def lead_edit_form(request: Request, lead_id: int, session: AsyncSession = Depends(get_session)):
    lead = await _load_lead(session, lead_id)
    return templates.TemplateResponse(request, "leads/form.html", _form_ctx(lead, await svc.list_tags(session)))


@router.post("/leads/{lead_id}/edit")
async def lead_update(request: Request, lead_id: int, session: AsyncSession = Depends(get_session)):
    lead = await _load_lead(session, lead_id)
    form = await _read_lead_form(request)
    try:
        await svc.update_lead(session, lead, **form)
    except svc.ValidationError as e:
        lead = await _load_lead(session, lead_id)
        ctx = _form_ctx(lead, await svc.list_tags(session), form, e.errors)
        return templates.TemplateResponse(request, "leads/form.html", ctx, status_code=422)
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/leads/{lead_id}/delete")
async def lead_delete(lead_id: int, session: AsyncSession = Depends(get_session)):
    await svc.delete_lead(session, await _load_lead(session, lead_id))
    return RedirectResponse("/leads", status_code=303)


# --- история и теги (HTMX)


@router.post("/leads/{lead_id}/notes")
async def lead_add_note(request: Request, lead_id: int, session: AsyncSession = Depends(get_session)):
    lead = await _load_lead(session, lead_id)
    text = str((await request.form()).get("text", ""))
    error = None
    try:
        await svc.add_note(session, lead, text)
    except svc.ValidationError as e:
        error = e.errors["text"]
    session.expunge_all()
    lead = await _load_lead(session, lead_id, with_messages=True)
    if not _is_htmx(request):
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)
    return templates.TemplateResponse(request, "partials/history.html", {"lead": lead, "note_error": error})


@router.post("/leads/{lead_id}/tags/{tag_id}")
async def lead_toggle_tag(request: Request, lead_id: int, tag_id: int, session: AsyncSession = Depends(get_session)):
    lead = await _load_lead(session, lead_id)
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(404, "Тег не найден")
    await svc.toggle_lead_tag(session, lead, tag)
    if not _is_htmx(request):
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/lead_tags.html",
        {"lead": lead, "all_tags": await svc.list_tags(session), "open": True},
    )
