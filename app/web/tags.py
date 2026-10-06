"""Справочник тегов: создание, переименование, цвет, удаление."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Tag
from app.services import leads as svc
from app.web.auth import require_user
from app.web.templating import templates

router = APIRouter(prefix="/tags", dependencies=[Depends(require_user)])


async def _render(request: Request, session: AsyncSession, *, errors: dict | None = None, form: dict | None = None,
                  status_code: int = 200):
    ctx = {
        "tags": await svc.list_tags(session),
        "usage": await svc.tag_usage(session),
        "errors": errors or {},
        "form": form or {"name": "", "color": svc.DEFAULT_TAG_COLOR},
    }
    return templates.TemplateResponse(request, "tags.html", ctx, status_code=status_code)


async def _load(session: AsyncSession, tag_id: int) -> Tag:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(404, "Тег не найден")
    return tag


@router.get("")
async def tags_page(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render(request, session)


@router.post("")
async def tag_create(request: Request, name: str = Form(""), color: str = Form(svc.DEFAULT_TAG_COLOR),
                     session: AsyncSession = Depends(get_session)):
    try:
        await svc.create_tag(session, name, color)
    except svc.ValidationError as e:
        return await _render(request, session, errors={"new": e.errors}, form={"name": name, "color": color},
                             status_code=422)
    return RedirectResponse("/tags", status_code=303)


@router.post("/{tag_id}")
async def tag_update(request: Request, tag_id: int, name: str = Form(""), color: str = Form(""),
                     session: AsyncSession = Depends(get_session)):
    tag = await _load(session, tag_id)
    try:
        await svc.update_tag(session, tag, name, color)
    except svc.ValidationError as e:
        await session.rollback()
        return await _render(request, session, errors={tag_id: e.errors}, status_code=422)
    return RedirectResponse("/tags", status_code=303)


@router.post("/{tag_id}/delete")
async def tag_delete(tag_id: int, session: AsyncSession = Depends(get_session)):
    await svc.delete_tag(session, await _load(session, tag_id))
    return RedirectResponse("/tags", status_code=303)
