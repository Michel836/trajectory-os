"""MVP — representative synthetic-portfolio seed.

This is a *synthetic* portfolio covering the kinds of domains a generic
execution tool supports (community planning, documentation, inventory,
administration, operations, learning and infrastructure). It is deliberately
free of personal identifiers, real amounts and real parties: deadlines are
relative to an injected ``today`` so the demo stays realistic without going
stale, and every fact the user does not yet know is left as an explicit
unknown.

Treat this as the starting dataset the user replaces with their real data.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from trajectory_os.intelligence import model as intel_model
from trajectory_os.mvp import model, store

SEED_NAME = "representative-portfolio"


def _d(today: date, offset: int) -> str:
    return (today + timedelta(days=offset)).isoformat()


def build_portfolio(*, today: date | None = None,
                    name: str = SEED_NAME) -> model.Portfolio:
    """Build the representative portfolio with relative deadlines."""
    today = today or date.today()
    stamp = intel_model.utc_now()

    projects = (
        model.Project(
            project_id="garden-planner", name="Community Garden Planner",
            objective="Build a planning tool for a community garden",
            status=model.PS_ACTIVE, domain="community",
            urgency=model.U_HIGH, impact=model.I_HIGH, deadline=_d(today, 21)),
        model.Project(
            project_id="docs-library", name="Open-Source Documentation Library",
            objective="Ship a searchable documentation library",
            status=model.PS_ACTIVE, domain="product",
            urgency=model.U_HIGH, impact=model.I_HIGH, deadline=_d(today, 3)),
        model.Project(
            project_id="inventory-tool", name="Fictional Inventory Management",
            objective="Track fictional stock and reorder points",
            status=model.PS_WAITING, domain="operations",
            urgency=model.U_MEDIUM, impact=model.I_HIGH, deadline=_d(today, 21)),
        model.Project(
            project_id="admin-renewal", name="Generic Administrative Renewal",
            objective="Complete a generic administrative renewal",
            status=model.PS_ACTIVE, domain="admin",
            urgency=model.U_HIGH, impact=model.I_HIGH, deadline=_d(today, 14)),
        model.Project(
            project_id="equipment-maintenance", name="Sample Equipment Maintenance",
            objective="Maintain the sample lab equipment",
            status=model.PS_ACTIVE, domain="admin",
            urgency=model.U_MEDIUM, impact=model.I_MEDIUM, deadline=_d(today, 12)),
        model.Project(
            project_id="training-application", name="Sample Training Application",
            objective="Submit a sample training application",
            status=model.PS_ACTIVE, domain="career",
            urgency=model.U_MEDIUM, impact=model.I_HIGH, deadline=_d(today, 20)),
        model.Project(
            project_id="conference-talk", name="Sample Conference Preparation",
            objective="Prepare a talk for a sample conference",
            status=model.PS_DEFERRED, domain="career",
            urgency=model.U_LOW, impact=model.I_MEDIUM),
        model.Project(
            project_id="crm-practice", name="Fictional Consulting CRM",
            objective="Keep a fictional consulting practice organised",
            status=model.PS_ACTIVE, domain="personal",
            urgency=model.U_MEDIUM, impact=model.I_MEDIUM),
        model.Project(
            project_id="budget-tracker", name="Synthetic Budget Tracker",
            objective="Keep a synthetic monthly budget current",
            status=model.PS_ACTIVE, domain="finance",
            urgency=model.U_MEDIUM, impact=model.I_HIGH),
        model.Project(
            project_id="home-lab-backup", name="Home Lab Backup",
            objective="Ensure the home lab is backed up and recoverable",
            status=model.PS_BLOCKED, domain="infrastructure",
            urgency=model.U_HIGH, impact=model.I_MEDIUM, deadline=_d(today, 14)),
        model.Project(
            project_id="community-roster", name="Community Volunteer Roster",
            objective="Maintain the community volunteer roster",
            status=model.PS_ACTIVE, domain="career",
            urgency=model.U_LOW, impact=model.I_MEDIUM),
        model.Project(
            project_id="language-study", name="Demo Language-Learning Project",
            objective="Study a language with a small demo tracker",
            status=model.PS_ACTIVE, domain="personal",
            urgency=model.U_LOW, impact=model.I_MEDIUM),
    )

    tasks = (
        model.Task(
            task_id="garden.plot-model", project_id="garden-planner",
            title="Define the plot and volunteer model",
            workstream="planning", deliverable="plot model",
            estimated_minutes=60, urgency=model.U_HIGH, impact=model.I_HIGH,
            next_action="List the plot and volunteer fields", created_at=stamp,
            updated_at=stamp),
        model.Task(
            task_id="garden.volunteer-list", project_id="garden-planner",
            title="List the volunteer shifts",
            workstream="research", deliverable="volunteer list",
            estimated_minutes=45, urgency=model.U_HIGH, impact=model.I_HIGH,
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="garden.invite", project_id="garden-planner",
            title="Invite five volunteers",
            workstream="volunteering", deliverable="volunteer invitations",
            estimated_minutes=30, urgency=model.U_MEDIUM, impact=model.I_HIGH,
            dependencies=("garden.volunteer-list",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="garden.build-board", project_id="garden-planner",
            title="Publish the planting board",
            workstream="volunteering", deliverable="planting board",
            estimated_minutes=90, urgency=model.U_HIGH, impact=model.I_HIGH,
            deadline=_d(today, 7),
            dependencies=("garden.plot-model", "garden.volunteer-list"),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="docs.index", project_id="docs-library",
            title="Ship the documentation index",
            workstream="library", deliverable="working index",
            estimated_minutes=240, urgency=model.U_CRITICAL,
            impact=model.I_HIGH, deadline=_d(today, 3),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="docs.demo", project_id="docs-library",
            title="Record the documentation demo",
            workstream="library", deliverable="demo",
            estimated_minutes=60, urgency=model.U_HIGH, impact=model.I_MEDIUM,
            deadline=_d(today, 4), dependencies=("docs.index",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="docs.readme", project_id="docs-library",
            title="Write the documentation README",
            workstream="library", deliverable="readme",
            estimated_minutes=60, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            dependencies=("docs.index",), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="stock.audit", project_id="inventory-tool",
            title="Collect the stock counts",
            workstream="audit", deliverable="stock counts",
            estimated_minutes=60, urgency=model.U_HIGH, impact=model.I_HIGH,
            waiting_for=("warehouse counts",), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="stock.review", project_id="inventory-tool",
            title="Review reorder points",
            workstream="audit", deliverable="review",
            estimated_minutes=90, urgency=model.U_MEDIUM, impact=model.I_HIGH,
            dependencies=("stock.audit",), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="stock.reorder", project_id="inventory-tool",
            title="Propose a reorder plan",
            workstream="reorder", deliverable="reorder plan",
            estimated_minutes=120, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            deadline=_d(today, 21), dependencies=("stock.review",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="renewal.docs", project_id="admin-renewal",
            title="Gather the renewal documents",
            workstream="preparation", deliverable="document set",
            estimated_minutes=60, urgency=model.U_HIGH, impact=model.I_HIGH,
            deadline=_d(today, 10), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="renewal.appt", project_id="admin-renewal",
            title="Schedule the renewal appointment",
            workstream="preparation", deliverable="appointment",
            estimated_minutes=15, urgency=model.U_CRITICAL, impact=model.I_HIGH,
            deadline=_d(today, 5), waiting_for=("office availability",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="renewal.confirm", project_id="admin-renewal",
            title="Confirm the renewal",
            workstream="execution", deliverable="confirmed renewal",
            estimated_minutes=45, urgency=model.U_HIGH, impact=model.I_HIGH,
            deadline=_d(today, 14),
            dependencies=("renewal.docs", "renewal.appt"),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="maintenance.compare", project_id="equipment-maintenance",
            title="Compare three maintenance options",
            workstream="renewal", deliverable="comparison",
            estimated_minutes=45, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            deadline=_d(today, 12), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="maintenance.choose", project_id="equipment-maintenance",
            title="Choose a maintenance plan and book it",
            workstream="renewal", deliverable="active plan",
            estimated_minutes=30, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            deadline=_d(today, 12), dependencies=("maintenance.compare",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="training.certificates", project_id="training-application",
            title="Gather the sample certificates",
            workstream="application", deliverable="certificates",
            estimated_minutes=30, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="training.submit", project_id="training-application",
            title="Submit the sample application",
            workstream="application", deliverable="application",
            estimated_minutes=90, urgency=model.U_HIGH, impact=model.I_HIGH,
            deadline=_d(today, 20), dependencies=("training.certificates",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="talk.abstract", project_id="conference-talk",
            title="Write the talk abstract",
            workstream="profile", deliverable="abstract",
            estimated_minutes=30, urgency=model.U_LOW, impact=model.I_MEDIUM,
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="talk.slides", project_id="conference-talk",
            title="Outline the slide deck",
            workstream="profile", deliverable="slide outline",
            estimated_minutes=15, urgency=model.U_LOW, impact=model.I_LOW,
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="crm.inbox", project_id="crm-practice",
            title="Process the practice inbox",
            workstream="routine", deliverable="empty inbox",
            estimated_minutes=45, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            deadline=_d(today, 1), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="crm.review", project_id="crm-practice",
            title="Run the practice review",
            workstream="routine", deliverable="practice review",
            estimated_minutes=30, urgency=model.U_LOW, impact=model.I_MEDIUM,
            deadline=_d(today, 7), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="budget.categories", project_id="budget-tracker",
            title="Collect the budget categories",
            workstream="categories", deliverable="category list",
            estimated_minutes=60, urgency=model.U_HIGH, impact=model.I_HIGH,
            deadline=_d(today, 30), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="budget.review", project_id="budget-tracker",
            title="Review the monthly budget",
            workstream="budget", deliverable="budget review",
            estimated_minutes=30, urgency=model.U_MEDIUM, impact=model.I_MEDIUM,
            deadline=_d(today, 3), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="lab.audit", project_id="home-lab-backup",
            title="Audit backup coverage",
            workstream="backup", deliverable="audit",
            estimated_minutes=45, urgency=model.U_HIGH, impact=model.I_MEDIUM,
            deadline=_d(today, 7), created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="lab.setup", project_id="home-lab-backup",
            title="Configure the offsite backup",
            workstream="backup", deliverable="offsite backup",
            estimated_minutes=90, urgency=model.U_HIGH, impact=model.I_MEDIUM,
            deadline=_d(today, 14), dependencies=("lab.audit",),
            resources=("computer", "offsite-storage"),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="roster.messages", project_id="community-roster",
            title="Send five roster messages",
            workstream="outreach", deliverable="messages",
            estimated_minutes=30, urgency=model.U_LOW, impact=model.I_MEDIUM,
            blocked_by=("garden.plot-model",),
            created_at=stamp, updated_at=stamp),
        model.Task(
            task_id="study.schedule", project_id="language-study",
            title="Schedule the study session",
            workstream="appointments", deliverable="study plan",
            estimated_minutes=15, urgency=model.U_LOW, impact=model.I_LOW,
            waiting_for=("study group schedule",), created_at=stamp,
            updated_at=stamp),
    )

    resources = (
        model.Resource(resource_id="computer", name="Computer",
                       kind=model.RK_TOOL, available=True),
        model.Resource(resource_id="internet", name="Internet access",
                       kind=model.RK_TOOL, available=True),
        model.Resource(resource_id="registrar", name="Registrar",
                       kind=model.RK_PERSON, available=True),
        model.Resource(resource_id="accountant", name="Accountant",
                       kind=model.RK_PERSON, available=True),
        model.Resource(resource_id="service-desk", name="Service desk",
                       kind=model.RK_PERSON, available=True),
        model.Resource(resource_id="offsite-storage", name="Offsite storage",
                       kind=model.RK_OTHER, available=False),
    )

    calendar = (
        model.CalendarEvent(
            event_id="focus-block", title="Focus block", day=_d(today, 0),
            start_minutes=540, end_minutes=660),
        model.CalendarEvent(
            event_id="study-group", title="Study group meeting",
            day=_d(today, 1), start_minutes=600, end_minutes=660),
    )

    return model.Portfolio(
        schema_version=model.SCHEMA_VERSION,
        name=name,
        projects=projects,
        tasks=tasks,
        resources=resources,
        calendar=calendar,
        capacity=model.Capacity(minutes_per_day=360, buffer_ratio=0.30),
        created_at=stamp,
        updated_at=stamp,
    ).validate()


def write_seed(root: str | Path, *, today: date | None = None,
               name: str = SEED_NAME) -> str:
    """Write the representative portfolio seed and return its path."""
    portfolio = build_portfolio(today=today, name=name)
    return store.save_portfolio(root, portfolio)


__all__ = ["SEED_NAME", "build_portfolio", "write_seed"]
