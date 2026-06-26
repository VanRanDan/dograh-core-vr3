"""TDD tests for resolve_billing_org_id — the shared gate/emitter org resolver.

The gate sites and the settlement emitter both resolve their org through this
helper so they reserve/settle against the SAME org. These tests seed a real
workflow under an org (via the transactional test session) and assert the
helper resolves it.
"""
import uuid

import pytest

from api.db.models import OrganizationModel, UserModel, WorkflowModel
from api.services.billing.enforcement import resolve_billing_org_id


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.mark.asyncio
async def test_none_workflow_id_returns_none(db_session):
    assert await resolve_billing_org_id(None) is None


@pytest.mark.asyncio
async def test_missing_workflow_returns_none(db_session):
    # An id that does not exist resolves to None (never raises).
    assert await resolve_billing_org_id(2_000_000_123) is None


@pytest.mark.asyncio
async def test_resolves_org_from_workflow_organization_id(db_session, async_session):
    org = OrganizationModel(provider_id=f"resolve-{_uid()}")
    async_session.add(org)
    await async_session.flush()

    wf = WorkflowModel(
        name=f"wf-{_uid()}",
        organization_id=org.id,
        workflow_definition={},
    )
    async_session.add(wf)
    await async_session.flush()

    assert await resolve_billing_org_id(wf.id) == org.id


@pytest.mark.asyncio
async def test_falls_back_to_owner_selected_org(db_session, async_session):
    """workflow.organization_id is NULL → resolve via owner's selected org."""
    org = OrganizationModel(provider_id=f"resolve-{_uid()}")
    async_session.add(org)
    await async_session.flush()

    user = UserModel(
        provider_id=f"u-{_uid()}",
        selected_organization_id=org.id,
    )
    async_session.add(user)
    await async_session.flush()

    wf = WorkflowModel(
        name=f"wf-{_uid()}",
        organization_id=None,
        user_id=user.id,
        workflow_definition={},
    )
    async_session.add(wf)
    await async_session.flush()

    assert await resolve_billing_org_id(wf.id) == org.id
