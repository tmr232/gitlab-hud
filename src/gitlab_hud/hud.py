from __future__ import annotations

import json
import os
import shutil
from itertools import count, islice
from operator import attrgetter
from pathlib import Path
from typing import List, Optional, TypeVar, Callable, Union

import attr
import cattr
import gitlab
import maya
import typer as typer
from diskcache import Index
from rich.console import Console
from rich.table import Table


@attr.frozen
class Config:
    my_username: str
    project_id: int

    @staticmethod
    def load(path: Union[str, Path]) -> Config:
        with open(path) as f:
            raw = json.load(f)
        return cattr.GenConverter().structure(raw, Config)

    @staticmethod
    def generate_default_json():
        return json.dumps(cattr.unstructure(Config("", 0)), indent=4)


g_config = Config("", 0)

DATA_ROOT = Path.home() / ".gitlab-hud"
CONFIG_PATH = DATA_ROOT / "config.json"

CACHE_ROOT = DATA_ROOT / "cache"
CONTROL_CACHE = str(CACHE_ROOT / "control")
MR_CACHE = str(CACHE_ROOT / "mrs")


def my_username():
    return g_config.my_username


def project_id():
    return g_config.project_id


_T = TypeVar("_T")
_U = TypeVar("_U")


def apply(f: Callable[[_T], _U], maybe: Optional[_T]) -> Optional[_U]:
    if maybe is None:
        return None
    return f(maybe)


@attr.frozen
class Update:
    author: User
    content: str
    updated_at: maya.MayaDT = attr.ib(repr=maya.MayaDT.slang_time)


@attr.frozen
class User:
    name: str
    username: str
    id: int

    @staticmethod
    def from_gitlab(user) -> User:
        name = user["name"]
        username = user["username"]
        id_ = user["id"]
        return User(name=name, username=username, id=id_)


@attr.frozen
class Pipeline:
    id: int
    status: str
    updated_at: maya.MayaDT = attr.ib(repr=maya.MayaDT.slang_time)
    link: str

    @staticmethod
    def from_gitlab(pipeline) -> Pipeline:
        id_ = pipeline["id"]
        status = pipeline["status"]
        updated_at = maya.MayaDT.from_iso8601(pipeline["updated_at"])
        link = pipeline["web_url"]

        return Pipeline(
            id=id_,
            status=status,
            updated_at=updated_at,
            link=link,
        )


@attr.frozen
class Note:
    system: bool
    author: User
    resolvable: bool
    resolved: Optional[bool]
    resolved_by: Optional[User]
    updated_at: maya.MayaDT = attr.ib(repr=maya.MayaDT.slang_time)
    body: str
    id: int
    type: Optional[str]

    @staticmethod
    def from_gitlab(note) -> Note:
        updated_at = maya.MayaDT.from_iso8601(note.updated_at)
        author = User.from_gitlab(note.author)
        resolvable = note.resolvable
        resolved = note.resolved if resolvable else None
        resolved_by = User.from_gitlab(note.resolved_by) if resolved else None
        body = note.body
        system = note.system
        id_ = note.id
        type_ = note.noteable_type

        return Note(
            system=system,
            author=author,
            resolvable=resolvable,
            resolved=resolved,
            resolved_by=resolved_by,
            body=body,
            updated_at=updated_at,
            id=id_,
            type=type_,
        )


@attr.frozen
class Approvals:
    approved: bool
    approved_by: List[User]
    updated_at: maya.MayaDT = attr.ib(repr=maya.MayaDT.slang_time)
    id: int

    @staticmethod
    def from_gitlab(approvals):
        approved = approvals.approved
        approved_by = [
            User.from_gitlab(approved_by["user"])
            for approved_by in approvals.approved_by
        ]
        updated_at = maya.MayaDT.from_iso8601(approvals.updated_at)
        id_ = approvals.id

        return Approvals(
            approved=approved,
            approved_by=approved_by,
            updated_at=updated_at,
            id=id_,
        )


@attr.frozen
class MergeRequest:
    title: str
    author: User
    ref: str
    link: str
    notes: List[Note]
    updated_at: maya.MayaDT = attr.ib(repr=maya.MayaDT.slang_time)
    merged_at: Optional[maya.MayaDT]
    closed_at: Optional[maya.MayaDT]
    approvals: Approvals
    id: int
    is_draft: bool
    pipelines: List[Pipeline]
    has_conflicts: bool

    @staticmethod
    def from_gitlab(merge_request) -> MergeRequest:
        author = User.from_gitlab(merge_request.author)
        title = merge_request.title
        ref = merge_request.references["full"]
        link = merge_request.web_url
        updated_at = maya.MayaDT.from_iso8601(merge_request.updated_at)
        closed_at = apply(maya.MayaDT.from_iso8601, merge_request.closed_at)
        merged_at = apply(maya.MayaDT.from_iso8601, merge_request.merged_at)
        notes = list(map(Note.from_gitlab, merge_request.notes.list(all=True)))
        approvals = Approvals.from_gitlab(merge_request.approvals.get())
        id_ = merge_request.id
        is_draft = merge_request.work_in_progress
        pipelines = list(map(Pipeline.from_gitlab, merge_request.pipelines()))
        has_conflicts = merge_request.has_conflicts

        return MergeRequest(
            title=title,
            author=author,
            ref=ref,
            link=link,
            notes=notes,
            updated_at=updated_at,
            closed_at=closed_at,
            merged_at=merged_at,
            approvals=approvals,
            id=id_,
            is_draft=is_draft,
            pipelines=pipelines,
            has_conflicts=has_conflicts,
        )

    def get_last_update(self) -> Update:
        def _is_relevant(note: Note):
            return note.resolvable or note.system

        relevant_notes = list(filter(_is_relevant, self.notes))

        if not relevant_notes:
            return Update(
                author=self.author,
                content="MR Created",
                updated_at=self.updated_at,
            )

        last_note = max(relevant_notes, key=attrgetter("updated_at"))
        content = last_note.body
        if last_note.system:
            content = content.splitlines()[0]

        return Update(
            author=last_note.author,
            content=content,
            updated_at=last_note.updated_at,
        )

    def get_last_pipeline(self) -> Pipeline:
        return max(self.pipelines, key=attrgetter("updated_at"))


def is_important(merge_request: MergeRequest):
    if merge_request.merged_at or merge_request.closed_at:
        return False

    if merge_request.get_last_update().author.username != my_username():
        return True

    return False


def fetch_merge_requests(project):
    for page in count(start=1):
        for merge_request in project.mergerequests.list(
            page=page,
            per_page=10,
            order_by="updated_at",
            state="opened",
            target_branch="master",
        ):
            yield merge_request


def load_merge_requests(project):
    merge_requests = Index(MR_CACHE)
    control = Index(CONTROL_CACHE)
    control.setdefault("updated_at", maya.MayaDT(0))
    updated_at = control.get("updated_at", maya.MayaDT(0))
    converter = get_converter()
    for gl_merge_request in islice(fetch_merge_requests(project), 50):
        if maya.MayaDT.from_iso8601(gl_merge_request.updated_at) <= updated_at:
            break
        merge_request = MergeRequest.from_gitlab(gl_merge_request)
        control["updated_at"] = max(control["updated_at"], merge_request.updated_at)
        merge_requests[merge_request.id] = json.dumps(
            converter.unstructure(merge_request)
        )
        yield merge_request

    for unstructured_merge_request in merge_requests.values():
        loads = json.loads(unstructured_merge_request)
        merge_request = converter.structure(loads, MergeRequest)
        if merge_request.updated_at <= updated_at:
            yield merge_request


def get_important_mrs(project, include_drafts: bool = False):
    for merge_request in load_merge_requests(project):
        if merge_request.is_draft and not include_drafts:
            continue
        if is_important(merge_request):
            yield merge_request


def color_by_update_time(updated_at: maya.MayaDT):
    if (maya.now() - updated_at).days < 1:
        return "green"

    if (maya.now() - updated_at).days < 7:
        return "yellow"

    return "red"


def get_converter():
    converter = cattr.GenConverter()

    converter.register_unstructure_hook(maya.MayaDT, maya.MayaDT.iso8601)
    converter.register_structure_hook(
        maya.MayaDT, lambda mdt, _: maya.MayaDT.from_iso8601(mdt)
    )

    return converter


def format_pipeline(pipeline: Pipeline):
    PIPELINE_ICONS = {
        "canceled": ":yellow_circle:",
        "running": ":blue_circle:",
        "skipped": ":white_circle:",
        "failed": ":red_circle:",
        "success": ":green_circle:",
    }

    return f"[link={pipeline.link}]{PIPELINE_ICONS[pipeline.status]}[/link]"


def format_title(merge_request: MergeRequest):
    if merge_request.has_conflicts:
        return f":exclamation_mark:{merge_request.title}"

    return merge_request.title


def display_hud(include_drafts):
    gl = gitlab.Gitlab.from_config()
    gl.auth()

    project = gl.projects.get(project_id())
    mr_table = Table(
        "Author",
        "Title",
        "Link",
        "Last Update",
        "CI",
        "Last Change",
    )

    for merge_request in sorted(
        get_important_mrs(project, include_drafts=include_drafts),
        key=attrgetter("updated_at"),
        reverse=True,
    ):
        update = merge_request.get_last_update()
        mr_table.add_row(
            merge_request.author.name,
            format_title(merge_request),
            f"[link={merge_request.link}]{merge_request.ref}[/link]",
            merge_request.updated_at.slang_time(),
            format_pipeline(merge_request.get_last_pipeline()),
            f"{update.author.name}: {update.content}",
            style=color_by_update_time(merge_request.updated_at),
        )

    console = Console()
    console.print(mr_table)


def _clear_cache():
    os.makedirs(DATA_ROOT, exist_ok=True)
    shutil.rmtree(CACHE_ROOT, ignore_errors=True)


def _setup_config():
    os.makedirs(DATA_ROOT, exist_ok=True)
    if not CONFIG_PATH.is_file():
        with CONFIG_PATH.open("w") as f:
            f.write(Config.generate_default_json())
    typer.edit(filename=CONFIG_PATH)


def main(
    include_drafts: bool = typer.Option(
        default=False,
        help="Include drafts and work-in-progress MRs in the HUD",
    ),
    setup: bool = typer.Option(
        default=False,
        help="Configure the HUD. Will happen automatically on first run.",
    ),
    clear_cache: bool = typer.Option(
        default=False,
        help="Clear the cache.",
    ),
):
    if setup:
        _setup_config()
        return

    elif clear_cache:
        _clear_cache()
        return

    global g_config
    g_config = Config.load(CONFIG_PATH)

    display_hud(include_drafts)


def entry_point():
    typer.run(main)


if __name__ == "__main__":
    entry_point()
