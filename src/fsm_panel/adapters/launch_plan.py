"""Pure preparation of the statically recovered Steam/CS2 launch core.

No host environment, filesystem, registry, process, clock or random source is
accessed. Plans describe legacy actions; there is deliberately no executor.
See ``docs/launch-plan.md`` for source addresses and reconstruction boundaries.
"""

import math
import ntpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

STEAM_ID_BASE = 76561197960265728
DETACHED_PROCESS = 0x00000008


class LaunchPlanError(ValueError):
    """Input validation introduced by the rebuild, with content-free codes."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _string(value: object) -> None:
    if type(value) is not str:
        raise LaunchPlanError("launch_string_required")


def _boolean(value: object) -> None:
    if type(value) is not bool:
        raise LaunchPlanError("launch_boolean_required")


def _integer(value: object) -> None:
    if type(value) is not int:
        raise LaunchPlanError("launch_integer_required")


@dataclass(frozen=True)
class LaunchSettings:
    """Explicit inputs; no field is an inferred legacy default.

    Dimensions accept plain integers or strings because their recovered use is
    empty-spec formatting, not arithmetic or resolution validation.
    """

    steam_path: str = field(repr=False)
    steam_launch_options: str = field(repr=False)
    additional_launch_options: str = field(repr=False)
    cs_width: int | str = field(repr=False)
    cs_height: int | str = field(repr=False)
    no_avast_mode: bool
    steam_launch_only: bool
    ancient_map_preload: bool
    running_2vs2: bool
    user_profile_new_link: str = field(repr=False)
    enable_shaders_caching: bool

    def __post_init__(self) -> None:
        for string_value in (
            self.steam_path,
            self.steam_launch_options,
            self.additional_launch_options,
            self.user_profile_new_link,
        ):
            _string(string_value)
        for dimension in (self.cs_width, self.cs_height):
            if type(dimension) not in (int, str):
                raise LaunchPlanError("launch_dimension_type")
        for flag in (
            self.no_avast_mode,
            self.steam_launch_only,
            self.ancient_map_preload,
            self.running_2vs2,
            self.enable_shaders_caching,
        ):
            _boolean(flag)


@dataclass(frozen=True)
class LaunchFacts:
    """Caller-supplied results for the legacy existence/link predicates."""

    launch_dota2_exists: bool
    shader_link_exists: bool
    shader_link_is_symlink: bool

    def __post_init__(self) -> None:
        _boolean(self.launch_dota2_exists)
        _boolean(self.shader_link_exists)
        _boolean(self.shader_link_is_symlink)


@dataclass(frozen=True)
class MakeDirectories:
    path: str = field(repr=False)
    exist_ok: Literal[True] = True


@dataclass(frozen=True)
class RemoveFile:
    """The recovered ``os.remove`` action, never recursive directory removal."""

    path: str = field(repr=False)


@dataclass(frozen=True)
class CreateDirectorySymlink:
    target: str = field(repr=False)
    link_path: str = field(repr=False)
    target_is_directory: Literal[True] = True


type ShaderOperation = MakeDirectories | RemoveFile | CreateDirectorySymlink


@dataclass(frozen=True)
class ShaderCachePlan:
    """One local try block; a failed action skips the rest of this group.

    ``caught_error`` records strict USERPROFILE lookup failure before any action.
    Runtime errors cannot be predicted from the injected existence facts.
    """

    operations: tuple[ShaderOperation, ...]
    caught_error: Literal["missing_userprofile"] | None = None
    on_exception: Literal["report_and_continue"] = "report_and_continue"


@dataclass(frozen=True)
class LaunchPlan:
    main: str = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] | None = field(repr=False)
    profile_directory: MakeDirectories | None
    shader_cache: ShaderCachePlan | None
    status: Literal["Starting"] = "Starting"
    used_port: Literal[21201] = 21201
    creationflags: int = DETACHED_PROCESS
    shell: Literal[False] = False
    profile_directory_on_exception: Literal["propagate"] = "propagate"

    def __post_init__(self) -> None:
        if self.environment is not None:
            object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


def build_launch_plan(
    settings: LaunchSettings,
    *,
    login: str,
    update_mode: bool,
    environment: Mapping[str, str],
    facts: LaunchFacts,
    ipc_random: int | None = None,
    steam_path: str | None = None,
) -> LaunchPlan:
    """Return literal arguments and conditional preparation actions.

    ``ipc_random`` is required only when NO_AVAST or UPDATE needs a suffix; the
    recovered inclusive randint range is 1..9999. ``steam_path=None`` selects the
    settings path; an empty override remains empty. Environment values are copied
    but never exposed in repr. ``environment=None`` on the result means the
    original Popen call omits its env keyword, including during UPDATE mode.
    """
    _string(login)
    _boolean(update_mode)
    if steam_path is not None:
        _string(steam_path)
    if not isinstance(environment, Mapping) or any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        raise LaunchPlanError("launch_environment_strings_required")

    # 0x14067d3c4..0x14067d7bb: rstrip, UPDATE prefix, conditional IPC suffix.
    steam_options = settings.steam_launch_options.rstrip()
    if update_mode:
        steam_options = "-login " + steam_options
    if settings.no_avast_mode or update_mode:
        if type(ipc_random) is not int or not 1 <= ipc_random <= 9999:
            raise LaunchPlanError("launch_ipc_random_range")
        steam_options += " -master_ipc_name_override " + format(login, "") + format(ipc_random, "")

    # 0x14067d7fe..0x14067eb58: retain substring matching and broad replacements.
    if not settings.steam_launch_only:
        main = (
            steam_options.rstrip()
            + " -applaunch 730 -language english +exec autoexec.cfg -w "
            + format(settings.cs_width, "")
            + " -h "
            + format(settings.cs_height, "")
            + " -console -condebug -conclearlog "
        )
        if facts.launch_dota2_exists:
            main = main.replace("730", "570")
        if settings.no_avast_mode:
            main += "-allowmultiple "
        main += "-con_logfile " + format(login, "") + ".log "
        main += settings.additional_launch_options
        new_params = ["-novid", "-nojoy"]
        if update_mode and settings.ancient_map_preload:
            new_params.append("+map de_inferno" if settings.running_2vs2 else "+map de_ancient")
        for param in new_params:
            if param not in main:
                main += " " + param
        for param in ("-d3d9ex", "-disable_d3d9_hacks", "-dxlevel 90"):
            if " " + param in main:
                main = main.replace(" " + param, "")
    else:
        main = steam_options.rstrip().replace("applaunch", "")

    # 0x14067eb64..0x14067ec83: split on ASCII space, preserving empty fields.
    executable = settings.steam_path if steam_path is None else steam_path
    argv = (format(executable, ""), *main.split(" "))

    # 0x14067ee4b..0x14067fbe4: copied env is passed only for NO_AVAST.
    child_environment = dict(environment)
    child_environment["VPROJECT"] = "1"
    profile_directory = None
    shader_cache = None
    if settings.no_avast_mode:
        profile = format(settings.user_profile_new_link, "") + "\\" + format(login, "")
        local = profile + "\\AppData\\Local"
        profile_directory = MakeDirectories(local)
        if not settings.enable_shaders_caching:
            # Strict indexing happens before parent creation or existence tests.
            if "USERPROFILE" not in environment:
                shader_cache = ShaderCachePlan((), caught_error="missing_userprofile")
            else:
                target = format(environment["USERPROFILE"], "") + "\\AppData\\Local\\NVIDIA"
                link_path = local + "\\NVIDIA"
                operations: list[ShaderOperation] = [MakeDirectories(ntpath.dirname(link_path))]
                if facts.shader_link_exists or facts.shader_link_is_symlink:
                    operations.append(RemoveFile(link_path))
                operations.append(CreateDirectorySymlink(target, link_path))
                shader_cache = ShaderCachePlan(tuple(operations))
        child_environment["USERPROFILE"] = profile
        child_environment["LOCALAPPDATA"] = local

    return LaunchPlan(
        main=main,
        argv=argv,
        environment=child_environment if settings.no_avast_mode else None,
        profile_directory=profile_directory,
        shader_cache=shader_cache,
    )


def userdata_id(steam_id: int) -> int:
    """Subtract the recovered base with no masking, clamping or int coercion.

    Exact-int validation is a rebuild boundary. The original can also dispatch
    subtraction on custom numeric objects; that broader domain is unsupported.
    Evidence: 0x140bfb190..0x140bfb3ce and 0x142d0d7b0..0x142d0d8c6.
    """
    _integer(steam_id)
    return steam_id - STEAM_ID_BASE


@dataclass(frozen=True)
class LoginusersPlan:
    """A VDF overwrite followed by a registry command, all descriptive only.

    The original shared try block reports Exception then proceeds to Popen.
    A failed VDF open/evaluation/write/close skips the registry command. ``content``
    is pre-write text: Windows text mode translates its LF newlines to CRLF.
    """

    path: str = field(repr=False)
    content: str = field(repr=False)
    registry_command: str = field(repr=False)
    timestamp: int
    encoding: Literal["utf-8"] = "utf-8"
    file_mode: Literal["w"] = "w"
    registry_shell: Literal[True] = True
    registry_capture_stdout: Literal[True] = True
    registry_capture_stderr: Literal[True] = True
    registry_text: Literal[True] = True
    registry_check: Literal[False] = False
    on_exception: Literal["report_and_continue"] = "report_and_continue"


def prepare_loginusers(
    settings_steam_path: str,
    *,
    steam_id: int,
    login: str,
    now: int | float,
) -> LoginusersPlan:
    """Render the legacy unescaped VDF and shell string without executing either.

    Pass the settings STEAM_PATH, even when launching with a different executable.
    The plain string/int and finite-time validations are rebuild decisions. Login
    text is deliberately not escaped or sanitized; this is review data only.
    Evidence: 0x14067fc23..0x140680852 within the recovered launch root.
    """
    _string(settings_steam_path)
    _string(login)
    _integer(steam_id)
    if type(now) not in (int, float) or (type(now) is float and not math.isfinite(now)):
        raise LaunchPlanError("launch_finite_time_required")
    timestamp = round(now - 180)
    command = (
        'reg add "HKCU\\Software\\Valve\\Steam" /v AutoLoginUser /t REG_SZ /d '
        + format(login, "")
        + " /f"
    )
    path = "\\".join(settings_steam_path.split("\\")[:-1]) + "\\config\\loginusers.vdf"
    # Explicitly reproduce the recovered layout without bundling original files.
    template = "\n".join(
        (
            "",
            '                    "users"',
            "                    {",
            '                        "%s"',
            "                        {",
            '                            "AccountName"        "%s"',
            '                            "PersonaName"        ""',
            '                            "RememberPassword"        "1"',
            '                            "WantsOfflineMode"        "0"',
            '                            "SkipOfflineModeWarning"        "0"',
            '                            "AllowAutoLogin"        "1"',
            '                            "MostRecent"        "1"',
            '                            "Timestamp"        "%s"',
            "                        }",
            "                    }",
            "                    ",
        )
    )
    content = template % (str(steam_id), login, str(timestamp))
    return LoginusersPlan(path, content, command, timestamp)
