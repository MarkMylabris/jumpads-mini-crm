import pytest

from app.models import LeadSource
from app.services import leads as svc


@pytest.fixture
async def tagged(session):
    vip = await svc.create_tag(session, "vip", "#d32f2f")
    hot = await svc.create_tag(session, "Горячий", "#ff9800")
    a = await svc.create_manual_lead(session, name="Alice", contact="a", tag_ids=[vip.id])
    b = await svc.create_manual_lead(session, name="Bob", contact="b", tag_ids=[vip.id, hot.id])
    c = await svc.create_manual_lead(session, name="Carol", contact="c", request="Хочу ЛЕНДИНГ", tag_ids=[hot.id])
    await svc.create_manual_lead(session, name="Dave", contact="d")
    return {"vip": vip, "hot": hot, "a": a, "b": b, "c": c}


async def names(session, **kw):
    page = await svc.list_leads(session, svc.LeadFilter(**kw))
    return sorted(lead.name for lead in page.items)


async def test_filter_by_one_tag(session, tagged):
    assert await names(session, tags=["vip"]) == ["Alice", "Bob"]
    assert await names(session, tags=["VIP"]) == ["Alice", "Bob"]  # без учёта регистра


async def test_filter_by_several_tags_any_and_all(session, tagged):
    assert await names(session, tags=["vip", "горячий"]) == ["Alice", "Bob", "Carol"]
    assert await names(session, tags=["vip", "горячий"], tag_mode="all") == ["Bob"]


async def test_filter_combined_with_status_source_and_search(session, tagged):
    assert await names(session, q="лендинг") == ["Carol"]  # кириллица без учёта регистра
    assert await names(session, tags=["горячий"], q="bob") == ["Bob"]
    assert await names(session, source=LeadSource.bot.value) == []
    assert await names(session, status="new", tags=["vip"]) == ["Alice", "Bob"]
    assert await names(session, tags=["missing"]) == []


async def test_direct_link_by_tag(auth_client, tagged):
    r = await auth_client.get("/leads?tag=vip")
    assert "Alice" in r.text and "Bob" in r.text and "Carol" not in r.text and "Dave" not in r.text


async def test_toggle_tag_from_list(auth_client, session, tagged):
    lead, hot = tagged["a"], tagged["hot"]
    r = await auth_client.post(f"/leads/{lead.id}/tags/{hot.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200 and f'id="lead-tags-{lead.id}"' in r.text
    assert await names(session, tags=["горячий"]) == ["Alice", "Bob", "Carol"]
    await auth_client.post(f"/leads/{lead.id}/tags/{hot.id}", headers={"HX-Request": "true"})
    session.expunge_all()
    assert await names(session, tags=["горячий"]) == ["Bob", "Carol"]


async def test_tag_crud(auth_client, session, tagged):
    vip = tagged["vip"]
    r = await auth_client.post("/tags", data={"name": "VIP", "color": "#000000"})
    assert r.status_code == 422 and "Такой тег уже есть" in r.text
    r = await auth_client.post("/tags", data={"name": "new", "color": "red"})
    assert r.status_code == 422 and "#rrggbb" in r.text

    assert (await auth_client.post(f"/tags/{vip.id}", data={"name": "premium", "color": "#123456"})).status_code == 303
    session.expunge_all()
    assert await names(session, tags=["premium"]) == ["Alice", "Bob"]

    assert (await auth_client.post(f"/tags/{vip.id}/delete")).status_code == 303
    session.expunge_all()
    assert await names(session, tags=["premium"]) == []
    lead = await svc.get_lead(session, tagged["b"].id)
    assert [t.name for t in lead.tags] == ["Горячий"]
