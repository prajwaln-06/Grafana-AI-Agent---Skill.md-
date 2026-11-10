import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(scope="session", autouse=True)
def isolated_database(tmp_path_factory):
    """Point the whole suite at a throwaway SQLite database.

    ``Settings.database_url`` defaults to the live PostgreSQL instance, so any
    test that boots the app (the OIDC / login-flow tests, for instance) would
    otherwise auto-register its stub identity into the development database and
    leave junk "pending" users behind. The env var wins over both the field
    default and the ``.env`` file, so it redirects every engine the suite
    creates.
    """
    db_path = tmp_path_factory.mktemp("rbac-db") / "test.db"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Drop anything already cached so no engine keeps pointing at the live DB.
    import app.config as config_mod
    import app.storage.database as database_mod

    config_mod._settings = None
    database_mod._engine = None
    database_mod._SessionFactory = None
    try:
        yield db_path
    finally:
        database_mod._engine = None
        database_mod._SessionFactory = None
        config_mod._settings = None
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def skills_root() -> Path:
    return BACKEND_ROOT / "skills"


@pytest.fixture()
def make_test_principal():
    """Build a realistic, server-side principal for a test.

    The identity is built the same way the production resolver builds one:
    an *authenticated*, active account with an explicit role and explicit
    resource scopes. Nothing about it is special-cased -- in particular it
    is not the development-anonymous principal, which represents "the
    service has authentication switched off" and would therefore not be a
    realistic stand-in for an authenticated caller.

    Use this factory to give a test the *minimum* role and scope its
    scenario needs::

        observer = make_test_principal("observer", folders=("fld-a",))
    """

    def _factory(
        role: str = "observer",
        *,
        user_id: str | None = None,
        issuer: str = "https://authentik.test",
        subject: str | None = None,
        status: str = "active",
        folders: tuple[str, ...] = (),
        dashboards: tuple[str, ...] = (),
        denied: tuple[str, ...] = (),
        environments: tuple[str, ...] = ("staging",),
        username: str | None = None,
    ):
        from app.auth.principal import Principal

        uid = user_id or f"usr_test_{role}"
        return Principal(
            user_id=uid,
            issuer=issuer,
            subject=subject or f"sub_{uid}",
            authenticated=True,
            status=status,
            role=role,
            allowed_folder_uids=frozenset(folders),
            allowed_dashboard_uids=frozenset(dashboards),
            denied_dashboard_uids=frozenset(denied),
            allowed_environments=frozenset(environments),
            username=username or uid,
        )

    return _factory


@pytest.fixture()
def api_principal(request):
    """Install an explicit, server-side identity on the API under test.

    Every protected endpoint of this service requires an authenticated
    identity, so a test that exercises application behaviour (rather than
    the authentication flow itself) states which identity it acts as. The
    principal is injected exactly the way the real dependency injects it --
    as the resolved server-side identity, including the request-scoped
    context binding the tool wrappers read -- so the authorization path
    under test is the production one.

    The default is a realistic *authenticated administrator*: active, with
    an explicit role and explicit scopes. It is not the development
    anonymous principal, because that one means "authentication is
    switched off" and would let a test pass for the wrong reason.

    This fixture deliberately does **not** weaken the application's own
    enforcement: it replaces the *identity resolution* step only. The
    role checks, the resource-scope checks and the status checks all still
    run, against the identity given here -- so a test that installs a
    non-administrator still gets HTTP 403 on an administrative endpoint,
    and a test that installs an inactive account still gets HTTP 403.

    Tests that exercise the authentication flow itself must not use this
    fixture; they call the endpoint without a session and assert HTTP 401.

    By default the principal is the development administrator. Tests that
    exercise a specific role parametrize this fixture indirectly::

        @pytest.mark.parametrize("api_principal", [my_principal], indirect=True)
    """
    from app.api.main import app
    from app.auth.dependencies import require_principal
    from app.auth.principal import Principal, set_current_principal

    default = Principal(
        user_id="usr_test_admin",
        issuer="https://authentik.test",
        subject="sub_usr_test_admin",
        authenticated=True,
        status="active",
        role="admin",
        allowed_folder_uids=frozenset({"*"}),
        allowed_environments=frozenset({"staging", "production"}),
        username="usr_test_admin",
    )
    principal = getattr(request, "param", None) or default

    # The legacy ADK gateway (run_server.py) exposes a second, smaller app
    # object; it carries the same administrative endpoints and therefore the
    # same guard.
    apps = [app]
    try:
        from run_server import app as legacy_app

        if legacy_app is not app:
            apps.append(legacy_app)
    except Exception:  # pragma: no cover - the legacy app is optional
        pass

    for target in apps:
        target.dependency_overrides[require_principal] = lambda: principal
    token = set_current_principal(principal)
    try:
        yield principal
    finally:
        for target in apps:
            target.dependency_overrides.pop(require_principal, None)
        set_current_principal(None)
        try:
            from app.auth.principal import CURRENT_PRINCIPAL
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            pass


@pytest.fixture(scope="session")
def skill_index(skills_root):
    from app.skill_index import SkillIndex
    return SkillIndex.load(skills_root)


@pytest.fixture()
def settings(skills_root):
    from app.config import Settings
    return Settings(
        gemini_api_key="test-key-not-real",
        skills_root=skills_root,
        prometheus_url="http://localhost:9090",
        opensearch_url="http://localhost:9600",
        # The structured metric catalog participates in the normal pipeline by
        # default (Router narrowing + Level 1/2/3), so ordinary tests that use
        # mocked Router/Generator responses and do not care about it would
        # otherwise pick up the real, committed app/catalog/catalog.json and
        # get catalog-driven retries layered onto their canned LLM replies.
        # Point at a path that can never exist so the catalog degrades to
        # "unavailable" (logged once) and those tests stay deterministic and
        # catalog-free. Catalog tests opt in explicitly (see
        # tests/test_pipeline_catalog_integration.py's `_settings`/`real_catalog`).
        catalog_path=skills_root.parent / "tests" / "__no_catalog_for_this_fixture__.json",
    )
