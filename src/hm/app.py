"""hm (harness-manager): manage Claude Code and Codex instances."""

from __future__ import annotations

import json
import os
from collections.abc import Callable

import tomlkit
from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.worker import get_current_worker

from hm import profiles, usage
from hm.profiles import Instance, Layer, Paths

ENGINES = ("claude", "codex")


def mark(value: bool | None) -> str:
    return "—" if value is None else "✔" if value else "✘"


def highlight(text: str, language: str) -> RenderableType:
    return Syntax(text, language, theme="ansi_dark", background_color="default",
                  word_wrap=True).highlight(text)


def fill(table: DataTable, rows: list[tuple[str, ...]], keys: list[str] | None = None) -> None:
    """Refill the table, keeping the cursor position where possible."""
    row = table.cursor_row
    table.clear()
    for i, cells in enumerate(rows):
        table.add_row(*cells, key=keys[i] if keys else None)
    if rows:
        table.move_cursor(row=min(row, len(rows) - 1))


class InstanceTable(DataTable):
    """Instance table; drag a row with the mouse to reorder."""

    drag_from: int | None = None

    def row_at(self, y: int) -> int | None:
        row = y + int(self.scroll_y) - (self.header_height if self.show_header else 0)
        return row if 0 <= row < self.row_count else None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.drag_from = self.row_at(event.y)
        if self.drag_from is not None:
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.drag_from is not None and (row := self.row_at(event.y)) is not None:
            self.move_cursor(row=row)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.drag_from is None:
            return
        self.release_mouse()
        source, self.drag_from = self.drag_from, None
        target = self.row_at(event.y)
        if target is not None and target != source:
            self.app.move_instance(source, target)


class LayerPanel(Vertical):
    """One layer's Setting, Plugin, Skill, MCP and Prompt; an instance panel also has Result,
    the outcome after layering with the shared layer."""

    def __init__(self, scope: str, *, id: str) -> None:
        super().__init__(id=id)
        self.scope = scope
        self.current: Layer | None = None
        self.plugins: list[profiles.Plugin] = []
        self.skills: list[str] = []

    def pane(self, part: str) -> str:
        return f"{self.scope}-{part}"

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Setting", id=self.pane("setting")), VerticalScroll():
                yield Static(classes="setting-view")
            with TabPane("Plugin", id=self.pane("plugin")):
                yield DataTable(cursor_type="row", classes="part-table plugin-table")
            with TabPane("Skill", id=self.pane("skill")):
                yield DataTable(cursor_type="row", classes="part-table skill-table")
            with TabPane("MCP", id=self.pane("mcp")):
                yield DataTable(cursor_type="row", classes="part-table mcp-table")
            with TabPane("Prompt", id=self.pane("prompt")), VerticalScroll():
                yield Static(classes="prompt-view")
            if self.scope == "instance":
                with TabPane("Result", id=self.pane("result")), VerticalScroll():
                    yield Static(classes="result-view")

    def on_mount(self) -> None:
        shared = self.scope == "shared"
        self.query_one(".plugin-table", DataTable).add_columns(
            *(("Plugin", "Version", "Shared") if shared
              else ("Plugin", "Version", "Instance", "Shared", "Effective")))
        self.query_one(".skill-table", DataTable).add_columns("Skill", "Directory")
        self.query_one(".mcp-table", DataTable).add_columns("MCP", "Transport", "URL / Command")

    @property
    def active_part(self) -> str:
        return self.query_one(TabbedContent).active.removeprefix(f"{self.scope}-")

    def activate(self, part: str) -> None:
        self.query_one(TabbedContent).active = self.pane(part)
        pane = self.query_one(f"#{self.pane(part)}", TabPane)
        target = next(iter(pane.query(DataTable)), None) or pane.query_one(VerticalScroll)
        target.focus()

    def show(self, layer: Layer | None, paths: Paths) -> None:
        self.current = layer
        if layer is None:
            for selector in (".setting-view", ".prompt-view", ".result-view"):
                for view in self.query(selector):
                    view.update("No instances yet")
            for table in self.query(DataTable):
                table.clear()
            return
        paths = profiles.for_engine(paths, layer.engine)
        self.show_setting(layer)
        self.show_plugins(layer, paths)
        self.show_skills(layer)
        self.show_mcp(layer, paths)
        self.show_prompt(layer)
        if isinstance(layer, Instance):
            self.show_result(layer, paths)

    def show_setting(self, layer: Layer) -> None:
        head = Text(f"{layer.settings_path}\n\n", style="bold")
        if layer.settings_error:
            body: RenderableType = Text(f"Failed to read: {layer.settings_error}", style="red")
        elif not layer.settings_path.exists():
            body = Text("The file does not exist yet; create it from the Actions menu", style="dim")
        else:
            body = highlight(layer.settings_path.read_text(), layer.format)
        self.query_one(".setting-view", Static).update(Group(head, body))

    def show_plugins(self, layer: Layer, paths: Paths) -> None:
        self.plugins = profiles.list_plugins(paths, layer)
        if layer.shared:
            rows = [(p.id, p.version, mark(p.enabled)) for p in self.plugins]
        else:
            shared = profiles.plugin_states(profiles.load_shared(paths))
            try:
                active = set(profiles.resolve(paths, layer).plugins)
            except (OSError, ValueError):
                active = set()
            rows = [(p.id, p.version, mark(p.enabled), mark(shared.get(p.id)),
                     "✔" if p.id in active else "") for p in self.plugins]
        fill(self.query_one(".plugin-table", DataTable), rows)

    def show_skills(self, layer: Layer) -> None:
        self.skills = profiles.list_skills(layer)
        fill(self.query_one(".skill-table", DataTable),
             [(name, str(layer.skills_dir / name)) for name in self.skills])

    def show_mcp(self, layer: Layer, paths: Paths) -> None:
        try:
            entries = [profiles.mcp_entry(k, v)
                       for k, v in sorted(profiles.list_mcp(paths, layer).items())]
            rows = [(e["name"], e["transport"], e["target"]) for e in entries]
        except (OSError, ValueError) as e:
            rows = [("Failed to read", "—", str(e))]
        fill(self.query_one(".mcp-table", DataTable), rows)

    def show_prompt(self, layer: Layer) -> None:
        where = (layer.prompt_path if layer.prompt_path is not None
                 else f"{layer.settings_path} · developer_instructions")
        text = profiles.read_prompt(layer)
        body = (highlight(text, "markdown") if text.strip()
                else Text("(empty) edit from the Actions menu", style="dim"))
        self.query_one(".prompt-view", Static).update(Group(Text(f"{where}\n\n", style="bold"),
                                                            body))

    def show_result(self, inst: Instance, paths: Paths) -> None:
        view = self.query_one(".result-view", Static)
        try:
            result = profiles.resolve(paths, inst)
        except (OSError, ValueError) as e:
            view.update(Text(f"Cannot layer: {e}", style="red"))
            return
        rule = ("Claude: shared layer (managed) wins; lists are unioned" if inst.engine == "claude"
                else "Codex: instance layer wins; tables merge key by key")
        parts: list[RenderableType] = [Text(f"Result = {inst.name} + Shared · {rule}\n",
                                            style="bold")]
        parts.append(Text("Plugin", style="bold underline"))
        parts.append(Text("\n".join(result.plugins) or "(none)"))
        parts.append(Text("\nSkill", style="bold underline"))
        parts.append(Text("\n".join(f"{n}  · {s}" for n, s in result.skills) or "(none)"))
        parts.append(Text("\nMCP", style="bold underline"))
        parts.append(Text("\n".join(f"{e['name']}  {e['transport']}  {e['target']}  · {e['source']}"
                                    for e in result.mcp) or "(none)"))
        parts.append(Text("\nPrompt", style="bold underline"))
        for title, text in result.prompt:
            parts += [Text(f"— {title} —", style="dim"), highlight(text, "markdown")]
        if not result.prompt:
            parts.append(Text("(none)"))
        parts.append(Text("\nSetting", style="bold underline"))
        settings = (tomlkit.dumps(result.settings) if inst.engine == "codex"
                    else json.dumps(result.settings, ensure_ascii=False, indent=2))
        parts.append(highlight(settings, inst.format))
        view.update(Group(*parts))


class EditScreen(Screen[bool]):
    BINDINGS = [  # noqa: RUF012
        Binding("ctrl+s", "save", "Save", key_display="Ctrl+S", priority=True),
        Binding("escape", "cancel", "Discard and go back", key_display="Esc", priority=True),
    ]

    def __init__(self, title: str, text: str, language: str,
                 save: Callable[[str], None]) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        self.language = language
        self.save = save

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f" Edit {self.title_text}", id="edit-title")
        yield TextArea.code_editor(self.text, language=self.language)
        yield Footer()

    def action_save(self) -> None:
        try:
            self.save(self.query_one(TextArea).text)
        except (OSError, ValueError) as e:
            self.notify(f"Not saved: {e}", severity="error")
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def compact(n: int) -> str:
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 \
        else f"{n / 1e3:.1f}K" if n >= 1e3 else str(n)


DIMENSION_NAMES = {"second": "Second", "hour": "Hour", "day": "Day", "week": "Week",
                   "month": "Month", "type": "Type", "model": "Model", "session": "Session"}


class ClickTable(DataTable):
    """A single click selects a row; by default DataTable's first click only moves the cursor."""

    async def _on_click(self, event: events.Click) -> None:
        row = event.style.meta.get("row", -1)
        if row < 0:  # header etc. fall through to the default handling
            return
        event.prevent_default()
        event.stop()
        self.move_cursor(row=row)
        self.action_select_cursor()


def money(cost: float | None) -> str:
    return "—" if cost is None else f"${cost:,.2f}" if cost >= 1 else f"${cost:.4f}"


def fill_usage(table: DataTable, rows: list[dict], by: list[str]) -> None:
    keys = [("time" if d in usage.BUCKETS else d) for d in by]
    keys += [] if "time" in keys else ["last"]
    table.clear(columns=True)
    table.add_columns(*({**DIMENSION_NAMES, "time": "Time", "last": "Last"}[k] for k in keys),
                      "Requests", "input", "output", "cache_read", "cache_write", "cost",
                      "speed", "first_token")
    for r in rows:
        table.add_row(
            *(r[k] for k in keys), f"{r['requests']:,}",
            *(compact(r[k]) for k in usage.PRICE_KEYS), money(r["cost"]),
            "—" if r["speed"] is None else f"{r['speed']} tok/s",
            "—" if r["first_token"] is None else f"{r['first_token'] / 1000:.1f}s",
        )


def usage_summary(title: str, rows: list[dict]) -> Text:
    unpriced = any(r["cost"] is None for r in rows)
    return Text.assemble(
        (f" {title}", "bold"),
        f" · {sum(r['requests'] for r in rows):,} requests"
        f" · {money(sum(r['cost'] or 0 for r in rows))}",
        (" (some models have no price; set prices from the Actions menu)" if unpriced else "",
         "yellow"),
    )


class SessionScreen(Screen[None]):
    """Every request of one Session in time order; requests within the same second share a row."""

    BINDINGS = [Binding("escape", "back", "Back", key_display="Esc")]  # noqa: RUF012

    def __init__(self, paths: Paths, session: str) -> None:
        super().__init__()
        self.paths = paths
        self.session = session

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="usage-summary")
        yield DataTable(cursor_type="row", id="usage")
        yield Footer()

    @work(thread=True, group="usage")
    def on_mount(self) -> None:
        rows = usage.report(self.paths, ["second", "model"], session=self.session)[::-1]
        self.app.call_from_thread(self.show, rows)

    def show(self, rows: list[dict]) -> None:
        self.query_one("#usage-summary", Static).update(
            usage_summary(f"Session {self.session}", rows))
        fill_usage(self.query_one("#usage", DataTable), rows, ["second", "model"])

    def action_back(self) -> None:
        self.dismiss()


class UsagePanel(Vertical):
    """Session log usage: filter by range, group by a time granularity and one dimension."""

    PERIODS = (("Today", 1), ("Last 7 days", 7), ("Last 30 days", 30), ("All", 0))
    BUCKETS = (("No time split", ""), ("By second", "second"), ("By hour", "hour"),
               ("By day", "day"), ("By week", "week"), ("By month", "month"))
    GROUPS = (("No grouping", ""), ("By type", "type"), ("By model", "model"),
              ("By Session", "session"))

    def __init__(self, paths: Paths) -> None:
        super().__init__()
        self.paths = paths
        self.rows: list[dict] = []
        self.by: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="usage-controls"):
            yield Select(self.PERIODS, value=7, allow_blank=False, id="period")
            yield Select(self.BUCKETS, value="", allow_blank=False, id="bucket")
            yield Select(self.GROUPS, value="session", allow_blank=False, id="group")
        yield Static(id="usage-summary")
        yield ClickTable(cursor_type="row", id="usage")

    def on_select_changed(self) -> None:
        if self.app.scope == "usage-tab":
            self.load()

    def load(self) -> None:
        self.by = [v for v in (self.query_one("#bucket", Select).value,
                               self.query_one("#group", Select).value) if v]
        self.rows = []
        self.query_one("#usage", DataTable).clear()
        self.query_one("#usage-summary", Static).update(
            " Computing… the first run has to read all session logs")
        self.fetch(list(self.by), self.query_one("#period", Select).value or None)

    @work(thread=True, exclusive=True, group="usage")
    def fetch(self, by: list[str], days: int | None) -> None:
        worker = get_current_worker()

        def show(rows: list[dict]) -> None:
            if not worker.is_cancelled:
                self.show(by, rows)

        try:
            rows = usage.report(self.paths, by, usage.since_days(days))
        except (OSError, ValueError) as e:
            self.app.call_from_thread(self.notify, f"Computation failed: {e}", severity="error")
            return
        self.app.call_from_thread(show, rows)

    def show(self, by: list[str], rows: list[dict]) -> None:
        self.rows = rows
        hint = " · click a row to see this session's requests" if "session" in by else ""
        self.query_one("#usage-summary", Static).update(usage_summary(f"Total{hint}", rows))
        fill_usage(self.query_one("#usage", DataTable), rows, by)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if "session" in self.by and self.rows:
            self.app.push_screen(SessionScreen(self.paths,
                                               self.rows[event.cursor_row]["session"]))

    def edit_pricing(self) -> None:
        def done(saved: bool | None) -> None:
            if saved:
                self.load()

        self.app.push_screen(EditScreen(
            str(self.paths.hm_dir / "pricing.json") + " (USD per million tokens)",
            usage.pricing_text(self.paths), "json",
            lambda text: usage.save_pricing(self.paths, text)), done)


class NewScreen(ModalScreen[tuple[str, Instance, str, str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]  # noqa: RUF012

    def __init__(self, paths: Paths, instances: list[Instance]) -> None:
        super().__init__()
        self.paths = paths
        self.instances = {i.name: i for i in instances}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("New instance")
            yield Input(placeholder="Name, e.g. claude-qwen, codex-glm", id="name")
            yield Select(
                [(f"{i.name} ({i.engine} · {i.provider})", i.name)
                 for i in self.instances.values()],
                prompt="Reuse common settings from which instance", id="template",
            )
            yield Input(placeholder="API URL (Responses API for Codex; empty if official)",
                        id="base-url")
            yield Input(placeholder="API token (empty for an official account)", id="token")
            yield Input(placeholder="Model, e.g. qwen3-coder-plus or opus", id="model")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        name = self.query_one("#name", Input).value.strip()
        template = self.query_one("#template", Select).value
        base_url = self.query_one("#base-url", Input).value.strip()
        token = self.query_one("#token", Input).value.strip()
        model = self.query_one("#model", Input).value.strip()
        if error := profiles.validate_new_name(self.paths, name):
            self.notify(error, severity="error")
        elif template not in self.instances:
            self.notify("Select an instance to copy", severity="error")
        elif not model or (base_url and not token):
            self.notify("Enter a model; third-party endpoints also need a token", severity="error")
        else:
            self.dismiss((name, self.instances[str(template)], base_url, token, model))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]  # noqa: RUF012

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.message)
            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        self.dismiss(False)


class HarnessManager(App[Instance | None]):
    TITLE = "hm"
    SUB_TITLE = "harness-manager"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    #problems { padding: 0 1; height: auto; }
    InstanceTable { height: auto; max-height: 40%; }
    #layers { height: auto; }
    LayerPanel { height: 1fr; border-top: solid $panel; }
    LayerPanel VerticalScroll { padding: 0 1; }
    .part-table { height: 1fr; }
    #edit-title { color: $text-muted; }
    #actions { width: 24; }
    #usage-controls { height: auto; }
    #usage-controls Select { width: 22; }
    #usage-summary { padding: 0 1; height: auto; }
    #usage { height: 1fr; }
    ModalScreen { align: center middle; }
    .dialog { width: 70; height: auto; max-height: 95%; overflow-y: auto;
              padding: 1 2; border: thick $primary; background: $surface; }
    .dialog > * { margin-bottom: 1; }
    .buttons { height: auto; align-horizontal: right; }
    .buttons Button { margin-left: 1; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=True)]  # noqa: RUF012

    def __init__(self, paths: Paths | None = None) -> None:
        super().__init__()
        self.paths = paths or Paths.default()
        self.instances: list[Instance] = []
        self.shared: dict[str, Layer] = {}
        self.logins: dict[str, str] = {}
        self.actions: dict[str, Callable[[], None]] = {}

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        with TabbedContent(id="top"):
            with TabPane("Instance", id="instance"), Vertical():
                yield Static(id="problems")
                yield InstanceTable(cursor_type="row", id="instances")
                yield LayerPanel("instance", id="instance-panel")
            with TabPane("Shared", id="shared"), Vertical():
                yield DataTable(cursor_type="row", id="layers")
                yield LayerPanel("shared", id="shared-panel")
            with TabPane("Usage", id="usage-tab"):
                yield UsagePanel(self.paths)
        yield Select([], prompt="Actions…", id="actions")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(InstanceTable)
        for label, key in [("Instance", "name"), ("Type", "kind"), ("Provider", "provider"),
                           ("Model", "model"), ("Login", "login")]:
            table.add_column(label, key=key)
        self.query_one("#layers", DataTable).add_columns(
            "Shared layer", "Setting", "Skill", "Status")
        self.refresh_data()
        table.focus()

    # ---------- Navigation ----------

    @property
    def scope(self) -> str:
        return self.query_one("#top", TabbedContent).active

    @property
    def panel(self) -> LayerPanel:
        return self.query_one(f"#{self.scope}-panel", LayerPanel)

    def update_actions(self) -> None:
        actions = {}
        if self.scope == "instance":
            actions = {"Launch instance": self.launch_selected, "New instance": self.new_instance,
                       "Delete instance": self.delete_selected}
        if self.scope == "usage-tab":
            actions["Set prices"] = self.query_one(UsagePanel).edit_pricing
        elif self.scope in ("instance", "shared"):
            part = self.panel.active_part
            if part in ("setting", "mcp", "prompt"):
                actions["Edit"] = self.edit_layer
            elif part == "plugin":
                actions["Enable / disable plugin"] = self.toggle_plugin
            elif part == "skill":
                if self.scope == "instance":
                    actions["Move skill to shared"] = self.promote_skill
                actions["Delete skill"] = self.delete_skill
        actions["Refresh"] = self.refresh_data
        self.actions = actions
        self.query_one("#actions", Select).set_options((label, label) for label in actions)

    @on(Select.Changed, "#actions")
    def select_action(self, event: Select.Changed) -> None:
        event.stop()
        if event.value is Select.NULL:
            return
        action = self.actions[str(event.value)]
        event.select.value = Select.NULL
        action()

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id == "top":
            if self.scope == "usage-tab":
                self.query_one(UsagePanel).load()
                self.query_one("#usage", DataTable).focus()
            else:
                self.query_one("#instances" if self.scope == "instance" else "#layers").focus()
        self.update_actions()

    # ---------- Data refresh ----------

    def refresh_data(self) -> None:
        if self.scope == "usage-tab":
            self.query_one(UsagePanel).load()
            return
        self.reload()
        self.check_logins()

    def reload(self) -> None:
        self.instances = profiles.list_all_instances(self.paths)
        self.shared = {e: profiles.load_shared(profiles.for_engine(self.paths, e))
                       for e in ENGINES}
        self.refresh_problems()
        self.refresh_instances()
        self.refresh_layers()

    @property
    def selected(self) -> Instance | None:
        table = self.query_one(InstanceTable)
        if not self.instances or table.cursor_row >= len(self.instances):
            return None
        return self.instances[table.cursor_row]

    @property
    def selected_shared(self) -> Layer:
        return self.shared[ENGINES[self.query_one("#layers", DataTable).cursor_row]]

    def login_text(self, inst: Instance) -> str:
        if not inst.is_official:
            return "API Key"
        return self.logins.get(inst.name, "Checking…")

    def refresh_problems(self) -> None:
        problems = [p for e in ENGINES
                    for p in profiles.find_problems(profiles.for_engine(self.paths, e))]
        text = Text()
        if problems:
            text.append(f"✘ {len(problems)} problem(s) found", style="bold red")
            for p in problems:
                text.append(f"\n  · {p}")
        view = self.query_one("#problems", Static)
        view.display = bool(problems)
        view.update(text)

    def refresh_instances(self, row: int | None = None) -> None:
        table = self.query_one(InstanceTable)
        if row is not None:
            table.move_cursor(row=row)
        fill(table, [(inst.name, inst.engine.capitalize(), inst.provider, inst.model,
                      self.login_text(inst)) for inst in self.instances],
             keys=[inst.name for inst in self.instances])
        self.show_instance()

    def refresh_layers(self) -> None:
        def state(layer: Layer) -> str:
            if layer.settings_error:
                return "Failed to read settings"
            return "Writable" if os.access(layer.settings_path.parent, os.W_OK) else "Not writable"

        fill(self.query_one("#layers", DataTable),
             [(layer.label, str(layer.settings_path), str(layer.skills_dir), state(layer))
              for layer in self.shared.values()])
        self.show_shared()

    def show_instance(self) -> None:
        self.query_one("#instance-panel", LayerPanel).show(self.selected, self.paths)

    def show_shared(self) -> None:
        self.query_one("#shared-panel", LayerPanel).show(self.selected_shared, self.paths)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "instances":
            self.show_instance()
        elif event.data_table.id == "layers":
            self.show_shared()

    @work(thread=True, exclusive=True, group="logins")
    def check_logins(self) -> None:
        for inst in [i for i in self.instances if i.is_official]:
            try:
                status = profiles.auth_status(inst)
                text = ((status.get("email") or "Logged in") if status.get("loggedIn")
                        else "Logged out")
            except Exception as e:
                text = f"Check failed: {e}"
            self.call_from_thread(self.set_login, inst.name, text)

    def set_login(self, name: str, text: str) -> None:
        self.logins[name] = text
        table = next(iter(self.query(InstanceTable)), None)
        if table is not None and name in table.rows:
            table.update_cell(name, "login", text, update_width=True)

    # ---------- Instance actions ----------

    def launch_selected(self) -> None:
        if inst := self.selected:
            self.exit(inst)

    def move_instance(self, source: int, target: int) -> None:
        names = [i.name for i in self.instances]
        names.insert(target, names.pop(source))
        profiles.save_order(self.paths, names)
        self.instances.insert(target, self.instances.pop(source))
        self.refresh_instances(row=target)

    def new_instance(self) -> None:
        def done(result: tuple[str, Instance, str, str, str] | None) -> None:
            if result is None:
                return
            name, template, base_url, token, model = result
            try:
                profiles.create_instance(profiles.for_engine(self.paths, template.engine),
                                         name, template, base_url, token, model)
            except (OSError, ValueError) as e:
                self.notify(f"Create failed: {e}", severity="error")
                return
            self.refresh_data()
            row = next(i for i, inst in enumerate(self.instances) if inst.name == name)
            self.query_one(InstanceTable).move_cursor(row=row)
            self.notify(f"Created {name}; launch it from the Actions menu")
            self.panel.activate("setting")
            self.edit_layer()

        self.push_screen(NewScreen(self.paths, self.instances), done)

    def delete_selected(self) -> None:
        inst = self.selected
        if inst is None:
            return
        if inst.is_default:
            self.notify(f"{inst.name} is the default instance (running {inst.engine} directly "
                        "uses it) and cannot be deleted", severity="warning")
            return
        extra = " and log the account out first" if inst.is_official else ""
        message = (f"Delete instance {inst.name}? This removes {inst.path}{extra}. "
                   "The shared layer is not affected.")

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                profiles.delete_instance(inst)
            except (OSError, ValueError) as e:
                self.notify(f"Delete failed: {e}", severity="error")
                return
            self.notify(f"Deleted {inst.name}")
            self.refresh_data()

        self.push_screen(ConfirmScreen(message), done)

    # ---------- Layer editing ----------

    def edit_layer(self) -> None:
        panel = self.panel
        layer, part = panel.current, panel.active_part
        if layer is None:
            return
        paths = profiles.for_engine(self.paths, layer.engine)
        codex = layer.engine == "codex"
        try:
            if part == "mcp":
                servers = profiles.list_mcp(paths, layer)
                text = (tomlkit.dumps({"mcp_servers": servers}) if codex
                        else json.dumps(servers, ensure_ascii=False, indent=2) + "\n")
                title, language = f"MCP of {layer.label}", layer.format

                def save(text: str) -> None:
                    data = tomlkit.parse(text).get("mcp_servers", {}) if codex else json.loads(text)
                    if not isinstance(data, dict):
                        raise ValueError("MCP config must be an object keyed by name")
                    profiles.save_mcp(paths, layer, data)
            elif part == "prompt":
                text, language = profiles.read_prompt(layer), "markdown"
                title = f"Prompt of {layer.label}"

                def save(text: str) -> None:
                    profiles.save_prompt(layer, text)
            elif part == "setting":
                path = layer.settings_path
                text = path.read_text() if path.exists() else ("" if codex else "{}\n")
                title, language = str(path), layer.format

                def save(text: str) -> None:
                    profiles.save_settings(layer, text)
            else:
                return
        except (OSError, ValueError) as e:
            self.notify(f"Failed to read: {e}", severity="error")
            return

        def done(saved: bool | None) -> None:
            if saved:
                self.notify(f"Saved {title}; takes effect in new sessions")
                self.reload()

        self.push_screen(EditScreen(title, text, language, save), done)

    def toggle_plugin(self) -> None:
        panel = self.panel
        table = panel.query_one(".plugin-table", DataTable)
        if panel.current is None or not panel.plugins:
            return
        plugin = panel.plugins[table.cursor_row]
        enabled = not plugin.enabled
        try:
            profiles.set_plugin(panel.current, plugin.id, enabled)
        except (OSError, ValueError) as e:
            self.notify(f"Save failed: {e}", severity="error")
            return
        self.reload()
        where = "the shared layer" if panel.current.shared else panel.current.name
        self.notify(f"{plugin.id} {'enabled' if enabled else 'disabled'} in {where}; "
                    "takes effect in new sessions")

    def promote_skill(self) -> None:
        panel = self.panel
        layer = panel.current
        if not isinstance(layer, Instance) or not panel.skills:
            return
        name = panel.skills[panel.query_one(".skill-table", DataTable).cursor_row]
        try:
            profiles.promote_skill(profiles.for_engine(self.paths, layer.engine), layer, name)
        except (OSError, ValueError) as e:
            self.notify(f"Move failed: {e}", severity="error")
            return
        self.reload()
        self.notify(f"{name} moved to the shared layer; every {layer.engine} instance can use it")

    def delete_skill(self) -> None:
        panel = self.panel
        layer = panel.current
        if layer is None or not panel.skills:
            return
        name = panel.skills[panel.query_one(".skill-table", DataTable).cursor_row]

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                profiles.delete_skill(layer, name)
            except OSError as e:
                self.notify(f"Delete failed: {e}", severity="error")
                return
            self.reload()
            self.notify(f"Deleted {name}")

        self.push_screen(ConfirmScreen(f"Delete skill {name} of {layer.label}? "
                                       f"This removes {layer.skills_dir / name}."), done)


def main() -> None:
    inst = HarnessManager().run()
    if inst is not None:
        os.execvpe(inst.engine, [inst.engine], inst.launch_env())
