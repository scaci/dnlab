import asyncio
from types import SimpleNamespace

from app.auth.deps import _role_satisfies
from app.auth.labs import can_create_lab, can_write_lab
from app.auth.models import AuthBackend, Lab, Role, User
from app.auth.assistant import block_assistant_gui


def _user(user_id: int, role: Role, *, backend: AuthBackend = AuthBackend.local_db) -> User:
    return User(id=user_id, username=f"user-{user_id}", role=role, backend=backend)


def test_assistant_satisfies_graduate_but_not_admin() -> None:
    assert _role_satisfies(Role.assistant, Role.graduate)
    assert _role_satisfies(Role.assistant, Role.student)
    assert not _role_satisfies(Role.assistant, Role.admin)


def test_assistant_lab_permissions_match_graduate() -> None:
    assistant = _user(1, Role.assistant)
    owner = _user(2, Role.student)
    lab = Lab(name="demo", owner_id=owner.id)

    assert can_create_lab(assistant)
    assert can_write_lab(assistant, lab, owner)
    assert not can_write_lab(assistant, lab, _user(3, Role.graduate))
    assert not can_write_lab(assistant, lab, _user(4, Role.admin))


def test_assistant_middleware_allows_api_and_blocks_static(monkeypatch) -> None:
    assistant = _user(1, Role.assistant)

    class Backend:
        async def resolve_request(self, *, cookies, headers):
            return assistant

    async def call_next(_request):
        return SimpleNamespace(status_code=204)

    monkeypatch.setattr("app.auth.assistant.get_backend", lambda: Backend())

    async def run_checks() -> None:
        api_response = await block_assistant_gui(
            SimpleNamespace(
                url=SimpleNamespace(path="/api/labs/"),
                cookies={},
                headers={},
            ),
            call_next,
        )
        assert api_response.status_code == 204

        gui_response = await block_assistant_gui(
            SimpleNamespace(url=SimpleNamespace(path="/"), cookies={}, headers={}),
            call_next,
        )
        assert gui_response.status_code == 403
        assert gui_response.body == b"assistant users are API-only"

        webui_response = await block_assistant_gui(
            SimpleNamespace(
                url=SimpleNamespace(path="/webui/token/"),
                cookies={},
                headers={},
            ),
            call_next,
        )
        assert webui_response.status_code == 403

    asyncio.run(run_checks())
