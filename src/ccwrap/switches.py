"""What the user can switch off in a new profile, and how.

This is the one place that maps an item of the request to the change that
switches it off (AGENTS.md, "The profile prompt"). The page only collects the
ticks and assembles the changes that this module names; it decides nothing.

Two inputs:
- the request, which says what is in the context and how big it is;
- the config the recording ran on (`config_dir` in ccwrap.json), read only for
  names, which says where an item comes from. The request alone cannot tell a
  user skill from a bundled one, the marketplace of a plugin, or the scope of
  an MCP server.

An item that cannot be traced to a row of the table is shown as "not
switchable here" with the reason, instead of being guessed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .reader import system_blocks

SKILLS_MARK = "The following skills are available for use with the Skill tool:"
AGENTS_MARK = "Available agent types for the Agent tool:"
MCP_MARK = "# MCP Server Instructions"
DEFERRED_RE = re.compile(r"^.*deferred tools are now available.*:\s*$", re.M | re.I)
CONNECTOR_PREFIX = "claude_ai_"
PLUGIN_PREFIX = "plugin_"
SYNCED_PREFIX = "anthropic-skills:"
ARTIFACT_TOOL = "Artifact"
TOOL_SEARCH = "ToolSearch"


def normalize(name: str) -> str:
    """A server name as it appears in tool names (code.claude.com/docs/en/mcp)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def server_of(tool: str) -> str | None:
    if not tool.startswith("mcp__"):
        return None
    rest = tool[len("mcp__") :]
    return rest.split("__", 1)[0] if "__" in rest else None


# --- the config the recording ran on -------------------------------------------


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def skill_names(base: Path) -> set[str]:
    """Skills in base/skills/NAME/SKILL.md and commands in base/commands/**.md."""
    names = set()
    skills = base / "skills"
    if skills.is_dir():
        for path in skills.iterdir():
            if path.name not in ("synced", ".trash") and (path / "SKILL.md").is_file():
                names.add(path.name)
    commands = base / "commands"
    if commands.is_dir():
        for path in commands.rglob("*.md"):
            names.add(":".join(path.relative_to(commands).with_suffix("").parts))
    return names


def enabled_plugins(settings: dict) -> list[str]:
    return [key for key, on in (settings.get("enabledPlugins") or {}).items() if on is True]


def read_config(meta: dict) -> dict:
    """What the page needs to know about the source config. Names only."""
    notes = []
    if meta.get("config_dir"):
        source = Path(meta["config_dir"])
    else:
        source = Path.home() / ".claude"
        notes.append(f"ccwrap.json has no config_dir: the profile copies from {source}, the default config.")
    if meta.get("home"):
        home = Path(meta["home"])
    else:
        home = Path(os.environ.get("CCWRAP_HOME") or Path.home() / ".ccwrap")
        notes.append(f"ccwrap.json has no home: the profile goes to {home / 'profiles'}.")
    if not source.is_dir():
        notes.append(f"The source config {source} does not exist on this machine now.")

    # The user's own config keeps .claude.json next to ~/.claude, not inside it.
    own = Path.home() / ".claude"
    is_own = os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(own))
    claude_json = Path.home() / ".claude.json" if is_own else source / ".claude.json"
    state = read_json(claude_json)

    project = Path(meta["project"]) if meta.get("project") else None
    if project is None:
        notes.append("ccwrap.json has no project: a project skill cannot be told from a bundled one.")
    local_servers: list[str] = []
    project_plugins: list[str] = []
    project_skills: set[str] = set()
    if project is not None:
        local_servers = list((((state.get("projects") or {}).get(str(project)) or {}).get("mcpServers") or {}))
        local_servers += list(read_json(project / ".mcp.json").get("mcpServers") or {})
        for name in ("settings.json", "settings.local.json"):
            project_plugins += enabled_plugins(read_json(project / ".claude" / name))
        project_skills = skill_names(project / ".claude")

    synced = set()
    synced_dir = source / "skills" / "synced"
    if synced_dir.is_dir():
        for account in synced_dir.iterdir():
            if account.is_dir():
                synced.update(p.name for p in account.iterdir() if (p / "SKILL.md").is_file())

    return {
        "source": str(source),
        "claude_json": str(claude_json),
        "own": is_own,
        "profiles": str(home / "profiles"),
        "sep": os.sep,
        "notes": notes,
        "installed": list((read_json(source / "plugins" / "installed_plugins.json").get("plugins") or {})),
        "enabled": enabled_plugins(read_json(source / "settings.json")),
        "project_plugins": project_plugins,
        "user_skills": skill_names(source),
        "project_skills": project_skills,
        "synced_skills": synced,
        "user_servers": list(state.get("mcpServers") or {}),
        "local_servers": local_servers,
    }


# --- what the request holds ------------------------------------------------------


def reminder_texts(body: dict) -> list[str]:
    """Texts the harness writes: system blocks, the system-role message, and
    system reminders. Tool results and the user's own words are left out, so a
    listing quoted in a file the user read is not counted."""
    texts = [b.get("text", "") for b in system_blocks(body) if isinstance(b, dict)]
    for message in body.get("messages") or []:
        content = message.get("content")
        parts = [content] if isinstance(content, str) else [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        for text in parts:
            if message.get("role") == "system" or text.lstrip().startswith("<system-reminder>"):
                texts.append(text)
    return texts


def listing_entries(text: str, start: int) -> list[tuple[str, str]]:
    """The "- NAME: description" entries that follow a mark, up to a blank line.
    An entry runs until the next entry; NAME ends at the first ": "."""
    entries = []
    body = text[start:].lstrip("\n")
    end = body.find("\n\n")
    block = body if end < 0 else body[: end + 1]
    current: list[str] = []
    for line in block.splitlines(keepends=True):
        if line.startswith("- ") and current:
            entries.append("".join(current))
            current = []
        if line.startswith("- ") or current:
            current.append(line)
    if current:
        entries.append("".join(current))
    out = []
    for entry in entries:
        name = entry[2:].split(": ", 1)[0].strip()
        out.append((name, entry))
    return out


def add(atoms: dict[str, int], key: str, chars: int) -> None:
    atoms[key] = atoms.get(key, 0) + chars


def scan(body: dict, text_size, wire_size) -> dict[str, int]:
    """Atom id -> characters in this request. An atom is one piece of the
    request that belongs to exactly one item: a tool schema, a line of the
    skill or agent listing, a server's instructions. Repeated listings add up."""
    atoms: dict[str, int] = {}
    tools = body.get("tools") or []
    for tool in tools:
        name = tool.get("name")
        if name:
            add(atoms, "tool:" + name, wire_size(tool))
    servers = {server_of(t.get("name", "")) for t in tools} - {None}

    for text in reminder_texts(body):
        at = text.find(SKILLS_MARK)
        if at >= 0:
            for name, entry in listing_entries(text, at + len(SKILLS_MARK)):
                add(atoms, "skill:" + name, text_size(entry))
        at = text.find(AGENTS_MARK)
        if at >= 0:
            for name, entry in listing_entries(text, at + len(AGENTS_MARK)):
                add(atoms, "agent:" + name, text_size(entry))
        found = DEFERRED_RE.search(text)
        if found:
            for line in text[found.end() :].lstrip("\n").splitlines(keepends=True):
                if not re.fullmatch(r"[A-Za-z0-9_.:-]+\s*", line):
                    break
                add(atoms, "tool:" + line.strip(), text_size(line))
                servers.add(server_of(line.strip()))
        at = text.find(MCP_MARK)
        if at >= 0:
            scan_instructions(text, at, servers - {None}, atoms, text_size)
    return atoms


def scan_instructions(text: str, at: int, servers: set[str], atoms: dict[str, int], text_size) -> None:
    """Per server, the "## NAME" section of the MCP instructions. A server's own
    text may hold "## " headings too, so only a heading that names a server
    with tools in the request starts a section. The block ends at the next
    top-level heading or at the next listing the harness appends."""
    block = text[at:]
    ends = [m.start() for m in re.finditer(r"^# ", block, re.M) if m.start() > 0]
    for mark in (SKILLS_MARK, AGENTS_MARK):
        found = block.find(mark)
        if found > 0:
            ends.append(found)
    block = block[: min(ends)] if ends else block
    current = None
    chunk: list[str] = []
    for line in block.splitlines(keepends=True):
        if line.startswith("## ") and normalize(line[3:].strip()) in servers:
            if current:
                add(atoms, "instr:" + current, text_size("".join(chunk)))
            current, chunk = normalize(line[3:].strip()), []
        if current:
            chunk.append(line)
    if current:
        add(atoms, "instr:" + current, text_size("".join(chunk)))


# --- items and their changes ---------------------------------------------------
#
# A change is a list of operations the page applies to the new profile:
#   ["deny", RULE]                 add RULE to permissions.deny in settings.json
#   ["set", KEY, VALUE]            top-level KEY in settings.json
#   ["map", KEY, NAME, VALUE]      NAME inside the object KEY in settings.json
#   ["drop_server", NAME]          leave NAME out of mcpServers in .claude.json
# This is the table in AGENTS.md, "The profile prompt", row by row.


def change_builtin(name: str) -> list:
    if name == ARTIFACT_TOOL:
        return [["set", "enableArtifact", False]]
    return [["deny", name]]


def change_mcp_tool(tool: str) -> list:
    return [["deny", tool]]


def change_server(server: str, user_servers: dict[str, str]) -> list:
    if server in user_servers:
        return [["drop_server", user_servers[server]]]
    return [["deny", "mcp__" + server]]


def change_connector(server: str) -> list:
    return [["deny", "mcp__" + server]]


def change_all_connectors() -> list:
    return [["set", "disableClaudeAiConnectors", True]]


def change_plugin(key: str) -> list:
    return [["map", "enabledPlugins", key, False]]


def change_user_skill(name: str) -> list:
    return [["map", "skillOverrides", name, "off"]]


def change_bundled_skills() -> list:
    return [["set", "disableBundledSkills", True]]


def change_subagent(name: str) -> list:
    return [["deny", f"Agent({name})"]]


def item(key, section, label, change, atoms=(), parent=None, note=None) -> dict:
    return {"id": key, "section": section, "label": label, "change": change, "atoms": list(atoms), "parent": parent, "note": note}


def plugin_of(prefix_or_server: str, installed: list[str], is_server: bool) -> str | None:
    """The installed plugin key NAME@MARKETPLACE that a skill prefix or a plugin
    MCP server name belongs to. None when no installed plugin, or more than one
    marketplace, has that name."""
    best: list[str] = []
    best_len = -1
    for key in installed:
        name = key.split("@", 1)[0]
        if is_server:
            hit = prefix_or_server.startswith(PLUGIN_PREFIX + normalize(name) + "_")
        else:
            hit = prefix_or_server == name
        if hit and len(name) > best_len:
            best, best_len = [key], len(name)
        elif hit and len(name) == best_len:
            best.append(key)
    return best[0] if len(best) == 1 else None


def build_items(atom_ids: list[str], config: dict) -> list[dict]:
    """Every switchable item seen in the run, in a fixed order, with the atoms
    it covers (its members' atoms included) and its change."""
    installed = config["installed"]
    user_servers = {normalize(name): name for name in config["user_servers"]}
    local_servers = {normalize(name) for name in config["local_servers"]}
    tools = [a[5:] for a in atom_ids if a.startswith("tool:")]
    skills = [a[6:] for a in atom_ids if a.startswith("skill:")]
    agents = [a[6:] for a in atom_ids if a.startswith("agent:")]
    instr = {a[6:] for a in atom_ids if a.startswith("instr:")}
    items: list[dict] = []
    plugin_atoms: dict[str, list[str]] = {}
    plugin_servers: dict[str, list[dict]] = {}

    # Built-in tools.
    for name in tools:
        if server_of(name) is None and not name.startswith("mcp__"):
            note = "switched off with enableArtifact" if name == ARTIFACT_TOOL else None
            items.append(item("tool:" + name, "builtin", name, change_builtin(name), ["tool:" + name], note=note))

    # MCP servers and their tools, connectors, plugin servers.
    by_server: dict[str, list[str]] = {}
    for name in tools:
        server = server_of(name)
        if server:
            by_server.setdefault(server, []).append(name)
    connectors = [s for s in by_server if s.startswith(CONNECTOR_PREFIX)]
    if connectors:
        all_atoms = []
        children = []
        for server in connectors:
            atoms = ["tool:" + t for t in by_server[server]] + (["instr:" + server] if server in instr else [])
            all_atoms += atoms
            children.append(item("server:" + server, "connectors", server[len(CONNECTOR_PREFIX) :].replace("_", " "), change_connector(server), atoms, parent="connectors"))
        items.append(item("connectors", "connectors", "all claude.ai connectors", change_all_connectors(), all_atoms))
        items += children
    for server, names in by_server.items():
        if server in connectors:
            continue
        own = ["instr:" + server] if server in instr else []
        atoms = own + ["tool:" + t for t in names]
        if server.startswith(PLUGIN_PREFIX):
            key = plugin_of(server, installed, is_server=True)
            if key:
                server_item = item("server:" + server, "plugins", server, change_server(server, user_servers), atoms, parent="plugin:" + key)
                plugin_servers.setdefault(key, []).append(server_item)
                plugin_atoms.setdefault(key, []).extend(atoms)
                continue
            note = "a plugin server, but not of a plugin installed in the source config: switched off by name only"
        elif server in user_servers:
            note = "user scope: left out of mcpServers"
        elif server in local_servers:
            note = "local or project scope: denied by name"
        else:
            note = "not in the source config (built in or added by Claude Code): denied by name"
        items.append(item("server:" + server, "mcp", server, change_server(server, user_servers), atoms, note=note))
        for t in names:
            items.append(item("tool:" + t, "mcp", t[len("mcp__" + server + "__") :], change_mcp_tool(t), ["tool:" + t], parent="server:" + server))

    # Skills: plugin skills go with their plugin, user skills one by one,
    # bundled skills as one group. The rest cannot be switched off from here.
    user, bundled, fixed = [], [], []
    for name in skills:
        prefix = name.split(":", 1)[0] if ":" in name else None
        if prefix is not None:
            key = plugin_of(prefix, installed, is_server=False)
            if key:
                plugin_atoms.setdefault(key, []).append("skill:" + name)
            elif name.startswith(SYNCED_PREFIX):
                fixed.append((name, "synced from claude.ai: switch it off in the claude.ai skill settings"))
            elif name in config["user_skills"]:
                user.append(name)
            elif name in config["project_skills"]:
                fixed.append((name, "project command: it lives in the repository, not in the profile"))
            else:
                fixed.append((name, f"'{prefix}' is not a plugin installed in the source config (synced from claude.ai or a directory-scoped skill)"))
        elif name in config["user_skills"]:
            user.append(name)
        elif name in config["project_skills"]:
            fixed.append((name, "project skill: it lives in the repository, not in the profile"))
        elif name in config["synced_skills"]:
            fixed.append((name, "synced from claude.ai: switch it off in the claude.ai skill settings"))
        else:
            bundled.append(name)
    for name in user:
        items.append(item("skill:" + name, "skills", name, change_user_skill(name), ["skill:" + name], note="user skill"))
    if bundled:
        items.append(
            item(
                "bundled", "skills", "bundled skills", change_bundled_skills(), ["skill:" + n for n in bundled],
                note="every listed skill that is not a user, project, plugin or synced skill; some of them may be built-in commands that stay",
            )
        )
        for name in bundled:
            items.append(item("member:" + name, "skills", name, None, ["skill:" + name], parent="bundled"))

    # Plugins: the enabled ones, and any installed one the request shows.
    for name in agents:
        prefix = name.split(":", 1)[0] if ":" in name else None
        key = plugin_of(prefix, installed, is_server=False) if prefix else None
        if key:
            plugin_atoms.setdefault(key, []).append("agent:" + name)
    keys = [k for k in config["enabled"]] + [k for k in plugin_atoms if k not in config["enabled"]]
    for key in keys:
        if key in config["project_plugins"]:
            note = "the project's settings enable it too, and they win over the profile: it stays on"
        elif not plugin_atoms.get(key):
            note = "nothing of it in the request"
        else:
            note = None
        items.append(item("plugin:" + key, "plugins", key, change_plugin(key), plugin_atoms.get(key, []), note=note))
        items += plugin_servers.get(key, [])

    # Subagent types.
    for name in agents:
        items.append(item("agent:" + name, "agents", name, change_subagent(name), ["agent:" + name]))

    for name, reason in fixed:
        items.append(item("fixed:" + name, "fixed", name, None, ["skill:" + name], note=reason))
    return items


def panel(atom_ids: list[str], config: dict) -> dict:
    """The data of the selection panel. Sets become lists so it goes into JSON."""
    return {
        "items": build_items(atom_ids, config),
        "source": config["source"],
        "claude_json": config["claude_json"],
        "own": config["own"],
        "profiles": config["profiles"],
        "sep": config["sep"],
        "notes": config["notes"],
        "user_servers": sorted(config["user_servers"]),
        "tool_search": "tool:" + TOOL_SEARCH,
    }

