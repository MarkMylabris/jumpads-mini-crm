from app.models import LeadSource, LeadStatus, MessageKind
from app.services import leads as svc


async def test_create_manual_lead_via_form(auth_client, session):
    tag = await svc.create_tag(session, "vip", "#ff0000")
    r = await auth_client.post(
        "/leads/new",
        data={"name": " Иван ", "contact": "+79990000000", "request": "Нужен сайт", "status": "new",
              "tag_ids": [str(tag.id)]},
    )
    assert r.status_code == 303
    lead_id = int(r.headers["location"].rsplit("/", 1)[1])

    lead = await svc.get_lead(session, lead_id)
    assert lead.name == "Иван"
    assert lead.source == LeadSource.manual
    assert lead.telegram_user_id is None
    assert [t.name for t in lead.tags] == ["vip"]

    page = await auth_client.get(f"/leads/{lead_id}")
    assert "Нужен сайт" in page.text and "vip" in page.text


async def test_required_fields_validation(auth_client):
    r = await auth_client.post("/leads/new", data={"name": "  ", "contact": "", "status": "new"})
    assert r.status_code == 422
    assert "Укажите имя" in r.text and "Укажите контакт" in r.text
    assert "Лидов пока нет" in (await auth_client.get("/leads")).text


async def test_edit_lead_and_status_change_logged(auth_client, session):
    lead = await svc.create_manual_lead(session, name="Олег", contact="@oleg")
    r = await auth_client.post(
        f"/leads/{lead.id}/edit",
        data={"name": "Олег П.", "contact": "@oleg", "request": "Лендинг", "status": "in_progress"},
    )
    assert r.status_code == 303
    session.expunge_all()
    lead = await svc.get_lead(session, lead.id, with_messages=True)
    assert (lead.name, lead.status) == ("Олег П.", LeadStatus.in_progress)
    assert lead.messages[-1].kind == MessageKind.system


async def test_add_note_htmx(auth_client, session):
    lead = await svc.create_manual_lead(session, name="Анна", contact="anna@example.com")
    r = await auth_client.post(f"/leads/{lead.id}/notes", data={"text": "Перезвонить в пятницу"},
                               headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="history"' in r.text and "Перезвонить в пятницу" in r.text

    r = await auth_client.post(f"/leads/{lead.id}/notes", data={"text": "  "}, headers={"HX-Request": "true"})
    assert "Пустая заметка" in r.text


async def test_list_is_newest_first_and_paginated(auth_client, session, settings, monkeypatch):
    monkeypatch.setattr(settings, "leads_per_page", 2)
    for i in range(3):
        await svc.create_manual_lead(session, name=f"Lead {i}", contact=f"c{i}")
    r = await auth_client.get("/leads")
    assert r.text.index("Lead 2") < r.text.index("Lead 1")
    assert "Lead 0" not in r.text and "Стр. 1 из 2" in r.text
    assert "Lead 0" in (await auth_client.get("/leads?page=2")).text


async def test_polling_returns_only_table(auth_client):
    r = await auth_client.get("/leads", headers={"HX-Request": "true", "HX-Target": "leads-table"})
    assert 'id="leads-table"' in r.text and "<html" not in r.text


async def test_delete_lead(auth_client, session):
    lead = await svc.create_manual_lead(session, name="X", contact="y")
    assert (await auth_client.post(f"/leads/{lead.id}/delete")).status_code == 303
    assert (await auth_client.get(f"/leads/{lead.id}")).status_code == 404
