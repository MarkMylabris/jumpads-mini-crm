async def test_pages_require_login(client):
    r = await client.get("/leads")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=/leads")


async def test_htmx_request_gets_hx_redirect(client):
    r = await client.get("/leads", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"] == "/login"


async def test_wrong_password(client):
    r = await client.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert "Неверный логин или пароль" in r.text


async def test_login_shows_empty_list_and_logout(auth_client):
    r = await auth_client.get("/leads")
    assert r.status_code == 200
    assert "Лидов пока нет" in r.text
    r = await auth_client.post("/logout")
    assert r.status_code == 303
    assert (await auth_client.get("/leads")).status_code == 303


async def test_open_redirect_is_blocked(client):
    r = await client.post("/login", data={"username": "admin", "password": "secret", "next": "//evil.com"})
    assert r.headers["location"] == "/leads"


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_login_throttled_after_repeated_failures(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    attacker = {"CF-Connecting-IP": "203.0.113.7"}
    for _ in range(3):
        r = await client.post("/login", data={"username": "admin", "password": "nope"}, headers=attacker)
        assert r.status_code == 401
    # пока блокировка, не пускает даже с верным паролем
    r = await client.post("/login", data={"username": "admin", "password": "secret"}, headers=attacker)
    assert r.status_code == 429 and "Слишком много попыток" in r.text
    # другого клиента блокировка не касается
    r = await client.post("/login", data={"username": "admin", "password": "secret"},
                          headers={"CF-Connecting-IP": "198.51.100.1"})
    assert r.status_code == 303


async def test_global_throttle_stops_ip_rotation(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 2)
    for i in range(2 * 4):  # GLOBAL_FACTOR = 4
        await client.post("/login", data={"username": "a", "password": "b"}, headers={"CF-Connecting-IP": f"10.0.0.{i}"})
    r = await client.post("/login", data={"username": "admin", "password": "secret"},
                          headers={"CF-Connecting-IP": "10.0.1.1"})
    assert r.status_code == 429


async def test_secure_cookie_when_https_only(settings, monkeypatch):
    import httpx

    from app.main import create_app

    monkeypatch.setattr(settings, "session_https_only", True)
    app = create_app(start_integrations=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/login", data={"username": "admin", "password": "secret"})
    cookie = r.headers["set-cookie"]
    assert "crm_session=" in cookie and "secure" in cookie.lower()
