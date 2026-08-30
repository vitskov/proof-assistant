from __future__ import annotations

from textual.widgets import Button, ContentSwitcher, OptionList

from proof_assistant.tui import ProofAssistantApp
from proof_assistant.tui.layout import ActionBar, PageWorkspace
from proof_assistant.tui.settings import SettingsHomeScreen
from tests.test_tui import FakeWorkflowService, async_test, wait_for
from tests.tui_geometry import (
    assert_inside_viewport,
    assert_regions_do_not_overlap,
    visible,
)


@async_test
async def test_settings_home_is_a_responsive_category_navigator() -> None:
    for size in ((80, 24), (120, 40), (140, 48)):
        service = FakeWorkflowService()
        app = ProofAssistantApp(service)

        async with app.run_test(size=size) as pilot:
            await wait_for(
                pilot,
                lambda: app.screen.__class__.__name__ == "WelcomeScreen",
            )
            app.show_settings(service.machine_settings)
            await wait_for(pilot, lambda: isinstance(app.screen, SettingsHomeScreen))
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, SettingsHomeScreen)
            workspace = screen.query_one("#settings-home-workspace", PageWorkspace)
            navigation = screen.query_one("#settings-category-nav", OptionList)
            pages = screen.query_one("#settings-category-pages", ContentSwitcher)
            actions = screen.query_one(ActionBar)

            assert navigation.option_count == 3
            assert pages.current == "settings-ai-category"
            assert_inside_viewport(app, workspace)
            assert_inside_viewport(app, actions)
            assert_regions_do_not_overlap(workspace, actions)

            if size[0] < 120:
                assert navigation.region.bottom <= pages.region.y
            else:
                assert navigation.region.right <= pages.region.x

            for index, (page_id, action_id) in enumerate(
                (
                    ("settings-ai-category", "open-ai-provider-settings"),
                    ("settings-runtime-category", "open-concurrency-settings"),
                    ("settings-advanced-category", "open-legacy-settings"),
                )
            ):
                navigation.highlighted = index
                navigation.focus()
                await pilot.press("enter")
                await pilot.pause()
                assert pages.current == page_id
                assert visible(screen.query_one(f"#{page_id}"))
                action = screen.query_one(f"#{action_id}", Button)
                assert visible(action)
                assert_inside_viewport(app, action)
