from __future__ import annotations

from dataclasses import replace

from textual.containers import VerticalScroll
from textual.widgets import Button, ContentSwitcher, OptionList, Select

from proof_assistant.ai import TaskKind
from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.layout import ActionBar, PageWorkspace
from proof_assistant.tui.screens import ClarificationScreen, ProgressScreen
from proof_assistant.tui.settings import (
    AIAccountVerificationConfirmationScreen,
    AIInstallConfirmationScreen,
    AIProviderSettingsScreen,
    ConcurrencyResourcesScreen,
)
from proof_assistant.tui.settings.components import RoleRoster
from proof_assistant.workflow.contracts import (
    ProgressEvent,
    ProgressPhase,
    WorkflowSnapshot,
    WorkflowState,
)
from tests.test_tui import FakeWorkflowService, clarification
from tests.test_tui_providers import (
    ProviderWorkflowFake,
    _snapshot,
    async_test,
    wait_for,
)
from tests.tui_geometry import (
    assert_focus_is_visible,
    assert_inside_viewport,
    assert_regions_do_not_overlap,
    visible,
)


@async_test
async def test_production_ai_role_editor_is_reachable_at_every_supported_size() -> None:
    for size in ((80, 24), (120, 40), (140, 48)):
        service = ProviderWorkflowFake(_snapshot())
        app = ProofAssistantApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=size) as pilot:
            await wait_for(pilot, lambda: app._ai_setup_snapshot is not None)
            app.show_ai_provider_settings(app._ai_setup_snapshot)
            await wait_for(
                pilot, lambda: isinstance(app.screen, AIProviderSettingsScreen)
            )
            screen = app.screen
            assert isinstance(screen, AIProviderSettingsScreen)
            await wait_for(
                pilot,
                lambda: (
                    screen.query_one("#ai-role-roster", RoleRoster).row_count
                    == len(TaskKind)
                ),
            )
            await pilot.pause()

            workspace = screen.query_one("#ai-settings-workspace", PageWorkspace)
            role_page = screen.query_one("#roles-page", VerticalScroll)
            actions = screen.query_one("#ai-settings-actions", ActionBar)
            roster = screen.query_one("#ai-role-roster", RoleRoster)

            assert workspace.styles.overflow_y == "hidden"
            assert role_page.styles.overflow_y in ("auto", "scroll")
            assert_inside_viewport(app, workspace)
            assert_inside_viewport(app, actions)
            assert_regions_do_not_overlap(workspace, actions)
            assert roster.row_count == 8
            if size[0] >= 120:
                assert roster.region.bottom <= actions.region.y
            else:
                assert role_page.max_scroll_y > 0

            roster.move_cursor(row=7, column=0, animate=False)
            roster.focus()
            roster.scroll_visible()
            await pilot.pause()
            assert_focus_is_visible(app)
            if size[0] < 140:
                await pilot.press("enter")
                await pilot.pause()
                assert not visible(roster)
                assert visible(screen.query_one("#ai-role-detail"))
            else:
                detail = screen.query_one("#ai-role-detail")
                assert visible(roster)
                assert visible(detail)
                assert roster.region.y == detail.region.y
                assert roster.region.right <= detail.region.x

            for selector in (
                "#ai-role-task",
                "#ai-role-model",
                "#ai-role-difficulty",
            ):
                control = screen.query_one(selector, Select)
                control.focus()
                control.scroll_visible()
                await pilot.pause()
                assert_focus_is_visible(app)
                assert role_page.region.contains_region(control.region)
                assert not actions.region.overlaps(control.region)

            if size[0] < 140:
                back = screen.query_one("#ai-role-detail-back", Button)
                back.focus()
                back.scroll_visible()
                await pilot.pause()
                assert_focus_is_visible(app)
                back.press()
                await pilot.pause()
                assert visible(roster)
                assert not visible(screen.query_one("#ai-role-detail"))


@async_test
async def test_first_run_wizard_keeps_each_step_and_actions_visible_at_80x24() -> None:
    service = ProviderWorkflowFake(_snapshot())
    app = ProofAssistantApp(service)  # type: ignore[arg-type]
    screen = AIProviderSettingsScreen(_snapshot(), first_run=True)

    async with app.run_test(size=(80, 24)) as pilot:
        app.switch_screen(screen)
        await wait_for(pilot, lambda: app.screen is screen)
        await wait_for(
            pilot,
            lambda: bool(screen.query("#ai-first-run-next").nodes),
        )

        workspace = screen.query_one("#ai-settings-workspace", PageWorkspace)
        actions = screen.query_one("#ai-settings-actions", ActionBar)
        pages = screen.query_one("#ai-settings-pages", ContentSwitcher)
        navigation = screen.query_one("#ai-settings-nav", OptionList)
        assert pages.current == "choose-page"
        assert navigation.highlighted == 0
        assert_inside_viewport(app, workspace)
        assert_inside_viewport(app, actions)
        assert_regions_do_not_overlap(workspace, actions)
        for selector in (
            "#ai-first-run-back",
            "#ai-first-run-next",
            "#recheck-ai-providers",
            "#ai-provider-back",
        ):
            assert_inside_viewport(app, screen.query_one(selector, Button))

        screen.query_one("#ai-first-run-next", Button).press()
        await wait_for(pilot, lambda: pages.current == "connection-page")
        assert navigation.highlighted == 1
        assert_regions_do_not_overlap(workspace, actions)
        assert_inside_viewport(app, screen.query_one("#ai-first-run-next", Button))

        screen.query_one("#ai-first-run-next", Button).press()
        await wait_for(pilot, lambda: pages.current == "roles-page")
        await wait_for(
            pilot,
            lambda: screen.query_one("#ai-role-roster", RoleRoster).row_count
            == len(TaskKind),
        )
        assert navigation.highlighted == 2
        assert_regions_do_not_overlap(workspace, actions)
        assert_inside_viewport(app, screen.query_one("#save-ai-settings", Button))
        assert_inside_viewport(app, screen.query_one("#ai-setup-continue", Button))


@async_test
async def test_long_confirmation_bodies_scroll_without_hiding_modal_actions() -> None:
    service = ProviderWorkflowFake(_snapshot())
    plan = replace(
        service.plan,
        detail=(service.plan.detail + " long diagnostic context") * 20,
        commands=service.plan.commands * 12,
    )
    for modal in (
        AIInstallConfirmationScreen(plan),
        AIAccountVerificationConfirmationScreen(),
    ):
        app = ProofAssistantApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for(
                pilot, lambda: app.screen.__class__.__name__ == "WelcomeScreen"
            )
            app.push_screen(modal)
            await wait_for(pilot, lambda: app.screen is modal)
            await pilot.pause()

            if isinstance(modal, AIInstallConfirmationScreen):
                body = modal.query_one("#ai-install-body", VerticalScroll)
                buttons = (
                    modal.query_one("#ai-install-cancel", Button),
                    modal.query_one("#ai-install-confirm", Button),
                )
                assert body.max_scroll_y > 0
            else:
                body = modal.query_one("#ai-account-check-body", VerticalScroll)
                buttons = (
                    modal.query_one("#ai-account-check-cancel", Button),
                    modal.query_one("#ai-account-check-confirm", Button),
                )
            assert_inside_viewport(app, body)
            for button in buttons:
                assert_inside_viewport(app, button)
                assert body.region.bottom <= button.region.y


@async_test
async def test_production_runtime_actions_stay_fixed_and_fully_visible() -> None:
    for size in ((80, 24), (120, 40), (140, 48)):
        service = FakeWorkflowService()
        app = ProofAssistantApp(service)
        async with app.run_test(size=size) as pilot:
            await wait_for(
                pilot, lambda: app.screen.__class__.__name__ == "WelcomeScreen"
            )
            app.show_concurrency_settings(service.machine_settings)
            await wait_for(
                pilot, lambda: isinstance(app.screen, ConcurrencyResourcesScreen)
            )
            screen = app.screen
            assert isinstance(screen, ConcurrencyResourcesScreen)
            await pilot.pause()

            workspace = screen.query_one("#runtime-settings-workspace", PageWorkspace)
            actions = screen.query_one("#runtime-settings-actions", ActionBar)
            navigation = screen.query_one("#runtime-settings-nav", OptionList)
            pages = screen.query_one("#runtime-settings-pages", ContentSwitcher)
            policy_page = screen.query_one("#runtime-policy-page", VerticalScroll)
            assert workspace.styles.overflow_y == "hidden"
            assert policy_page.max_scroll_y > 0
            assert_inside_viewport(app, workspace)
            assert_inside_viewport(app, actions)
            assert_regions_do_not_overlap(workspace, actions)

            for index, page_id in enumerate(
                (
                    "runtime-policy-page",
                    "runtime-overview-page",
                    "runtime-calibration-page",
                )
            ):
                navigation.highlighted = index
                navigation.focus()
                await pilot.press("enter")
                await pilot.pause()
                assert pages.current == page_id
                page = screen.query_one(f"#{page_id}", VerticalScroll)
                assert_inside_viewport(app, page)
                assert not actions.region.overlaps(page.region)

            for selector in (
                "#save-concurrency",
                "#reset-concurrency",
                "#concurrency-back",
            ):
                button = screen.query_one(selector, Button)
                button.focus()
                await pilot.pause()
                assert_focus_is_visible(app)


@async_test
async def test_production_progress_uses_compact_peers_and_wide_live_split() -> None:
    compact_sizes = {(80, 24), (120, 31), (140, 39)}
    for size in (*compact_sizes, (140, 48)):
        service = FakeWorkflowService()
        app = ProofAssistantApp(service)
        screen = ProgressScreen(
            "Verifying manuscript",
            project=service.project.project_path,
            cancellable=False,
            main_file="main.tex",
            input_files=("main.tex", "sections/proof.tex"),
            detached_job=True,
        )
        async with app.run_test(size=size) as pilot:
            app.switch_screen(screen)
            await wait_for(pilot, lambda: isinstance(app.screen, ProgressScreen))
            await wait_for(pilot, lambda: bool(screen.query("#progress-stages").nodes))
            screen.record_progress(
                ProgressEvent(1, ProgressPhase.INDEXING, "Indexed manuscript")
            )
            await pilot.pause()

            workspace = screen.query_one("#progress-workspace", PageWorkspace)
            actions = screen.query_one("#progress-actions-bar", ActionBar)
            switcher = screen.query_one("#progress-view-switcher")
            source_panel = screen.query_one("#progress-source-panel")
            stage_panel = screen.query_one("#progress-stage-panel")
            event_panel = screen.query_one("#progress-event-panel")
            assert_inside_viewport(app, workspace)
            assert_inside_viewport(app, actions)
            assert_regions_do_not_overlap(workspace, actions)

            for selector in ("#cancel", "#detach-observer"):
                button = screen.query_one(selector, Button)
                assert_inside_viewport(app, button)
                assert actions.region.contains_region(button.region)

            if size in compact_sizes:
                assert visible(switcher)
                assert not visible(source_panel)
                assert not visible(stage_panel)
                assert visible(event_panel)
                assert "Indexed manuscript" in screen.query_one("#progress-log").text

                await pilot.click("#show-progress-stages")
                await pilot.pause()
                assert not visible(source_panel)
                assert visible(stage_panel)
                assert not visible(event_panel)
                stages = screen.query_one("#progress-stages")
                stages.focus()
                await pilot.pause()
                assert_focus_is_visible(app)

                await pilot.click("#show-progress-sources")
                await pilot.pause()
                assert visible(source_panel)
                assert not visible(stage_panel)
                assert not visible(event_panel)
                sources = screen.query_one("#progress-sources")
                sources.focus()
                await pilot.pause()
                assert_focus_is_visible(app)
            else:
                assert not visible(switcher)
                assert visible(source_panel)
                assert visible(stage_panel)
                assert visible(event_panel)
                assert stage_panel.region.y == event_panel.region.y
                assert stage_panel.region.right <= event_panel.region.x
                assert not actions.region.overlaps(stage_panel.region)
                assert not actions.region.overlaps(event_panel.region)


@async_test
async def test_production_clarification_actions_fit_every_supported_size() -> None:
    compact_sizes = {(80, 24), (120, 31), (140, 39)}
    for size in (*compact_sizes, (120, 40), (140, 48)):
        service = FakeWorkflowService()
        snapshot = WorkflowSnapshot(
            WorkflowState.AWAITING_CLARIFICATION,
            service.project,
            clarifications=(clarification(service.project),),
        )
        app = ProofAssistantApp(service)
        async with app.run_test(size=size) as pilot:
            app.switch_screen(ClarificationScreen(snapshot))
            await wait_for(pilot, lambda: isinstance(app.screen, ClarificationScreen))
            screen = app.screen
            assert isinstance(screen, ClarificationScreen)
            await pilot.pause()

            workspace = screen.query_one(PageWorkspace)
            actions = screen.query_one("#clarification-actions", ActionBar)
            source_panel = screen.query_one("#clarification-source-panel")
            resolution_panel = screen.query_one("#clarification-resolution-panel")
            view_switcher = screen.query_one("#clarification-view-switcher")
            assert_inside_viewport(app, workspace)
            assert_inside_viewport(app, actions)
            assert_regions_do_not_overlap(workspace, actions)

            for selector in (
                "#open-folder",
                "#check-changes",
                "#previous",
                "#next",
            ):
                button = screen.query_one(selector, Button)
                assert_inside_viewport(app, button)
                assert actions.region.contains_region(button.region)
            assert not screen.query("#open-file")
            assert visible(screen.query_one("#source-excerpt"))

            if size in compact_sizes:
                assert visible(view_switcher)
                assert visible(source_panel)
                assert not visible(resolution_panel)

                await pilot.click("#show-clarification-resolution")
                await pilot.pause()
                assert not visible(source_panel)
                assert visible(resolution_panel)
                detail = screen.query_one("#clarification-detail")
                detail.focus()
                detail.scroll_visible()
                await pilot.pause()
                assert_focus_is_visible(app)
                assert not actions.region.overlaps(detail.region)
            else:
                assert not visible(view_switcher)
                assert visible(source_panel)
                assert visible(resolution_panel)
                assert source_panel.region.y == resolution_panel.region.y
                assert source_panel.region.right <= resolution_panel.region.x
