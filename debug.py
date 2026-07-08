import cmd
import enum
import re
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Dict, Match


@dataclass
class Breakpoint:
    number: int
    temp: bool = False


class StepMode(enum.Enum):
    NONE = 0
    STEP_INTO = 1
    STEP_OVER = 2
    STEP_OUT = 3


class DebugExit(Exception):
    pass


class DebugError(Exception):
    pass


class Debug(cmd.Cmd):
    prompt = "> "

    def __init__(self, path: Path = None) -> None:
        super().__init__()
        self.path = path
        self._exit = False
        self._running = False
        self._breakpoints: Dict[Path, Dict[int | str, Breakpoint]] = defaultdict(dict)
        self._step_mode = StepMode.NONE
        self._first_frame = None
        self._start_frame = None
        self._end_frame = None

    def start_prompt(self) -> None:
        if not self._exit:
            self.cmdloop()

    def do_start(self, _) -> bool:
        if not self.path:
            print("No file specified")
            return False

        if self._running:
            print("Debugging has already started")
            return False

        return True

    def do_continue(self, _) -> bool:
        if not self._running:
            print("Debugging has not started")
            return False
        return True

    def do_break(self, location: str) -> bool:
        try:
            self._add_breakpoint(location, temp=False)
        except DebugError as e:
            print(f"Failed to add breakpoint: {e}")
        return False

    def do_tbreak(self, location: str) -> bool:
        try:
            self._add_breakpoint(location, temp=True)
        except DebugError as e:
            print(f"Failed to add breakpoint: {e}")
        return False

    def _add_breakpoint(self, location: str, temp: bool) -> None:
        PATTERNS = [
            (r"(?P<line>\d+)", self._add_line_breakpoint),
            (r"(?P<func>\w+)", self._add_func_breakpoint),
            (r"(?P<filename>.+):(?P<line>\d+)", self._add_line_breakpoint),
            (r"(?P<filename>.+):(?P<func>\w+)", self._add_func_breakpoint),
        ]

        for pattern in PATTERNS:
            if m := re.fullmatch(pattern[0], location):
                pattern[1](m, temp)
                break

    def _add_line_breakpoint(self, match: Match[str], temp: bool):
        path, lines = self._get_lines(match.groupdict().get("filename"))

        line = int(match["line"])
        print(f"len: {len(lines)}")
        if 0 < line <= len(lines):
            self._create_breakpoint(path, line, temp)
        else:
            print("Invalid line number")

    def _add_func_breakpoint(self, match: Match[str], temp: bool):
        path, lines = self._get_lines(match.groupdict().get("filename"))

        # Regex adapted from Python's pdb implementation.
        FUNC_DEF = re.compile(r"^\s*def\s+(?P<func>[A-Za-z_]\w*)\s*\(")

        found = False
        func = match["func"]
        for line in lines:
            m = re.match(FUNC_DEF, line)
            if m and m["func"] == func:
                found = True

        if found:
            self._create_breakpoint(path, func, temp)
        else:
            print("Function not found")

    def _get_lines(self, filename: str | None) -> tuple[Path, list[str]]:
        try:
            path = self.path if not filename else Path(filename).resolve(strict=True)
            lines = path.read_text().splitlines(keepends=True)
        except OSError as e:
            raise DebugError(f"Failed to read file content") from e

        return path, lines

    def _create_breakpoint(self, path: Path, target: int | str, temp: bool) -> None:
        number = 0
        for filebps in self._breakpoints.values():
            for bp in filebps.values():
                if bp.number > number:
                    number = bp.number

        bp = Breakpoint(number + 1, temp)
        self._breakpoints[path][target] = bp

    def do_list(self, _) -> bool:
        if self._running:
            path, line = self._get_source_location(self._frame)
            self._show_lines(path, line, count=11)
        else:
            print("No source available")
        return False

    def do_locals(self, _) -> bool:
        locals = self._frame.f_locals
        if len(locals) > 0:
            width = 0
            for name in locals.keys():
                if len(name) > width:
                    width = len(name)
            for name, value in locals.items():
                print(f"{name:{width}} = {value}")
        else:
            print("No local variables")
        return False

    def do_next(self, _) -> bool:
        self._step_mode = StepMode.STEP_OVER
        self._start_frame = self._frame
        return True

    def do_step(self, _) -> bool:
        self._step_mode = StepMode.STEP_INTO
        return True

    def do_finish(self, _) -> bool:
        # If top-level frame, let it run to completion.
        if self._frame != self._first_frame:
            self._step_mode = StepMode.STEP_OUT
            self._end_frame = self._frame.f_back
        return True

    def do_info(self, arg: str) -> None:
        match arg:
            case "break":
                self._print_breakpoints()
            case _:
                print("Invalid info argument")
        return False

    def _print_breakpoints(self) -> None:
        if not self._breakpoints:
            print("No breakpoints specified")
            return

        breakpoints: list[tuple[Breakpoint, str]] = []
        for path, filebps in self._breakpoints.items():
            for line, bp in filebps.items():
                loc = f"{path}:{line}"
                breakpoints.append((bp, loc))

        breakpoints = sorted(breakpoints, key=lambda bp: bp[0].number)

        width = len(str(breakpoints[-1][0].number))
        for bp, loc in breakpoints:
            line = f"{bp.number:{width}}"
            line += "*" if bp.temp else " "
            line += f" at {loc}"
            print(line)

    def do_delete(self, arg: str) -> None:
        PATTERN = r"(?P<cmd>\w+)(?P<cmdarg> \w+)?"

        match = re.fullmatch(PATTERN, arg)
        if not match:
            print("Invalid command format")
            return False

        cmd = match.group("cmd")
        cmdarg = match.group("cmdarg")
        match cmd:
            case "break":
                self._run_delete_command(cmdarg)
            case _:
                print(f"Invalid delete argument {cmd}")
        return False

    def _run_delete_command(self, arg: str) -> None:
        if not arg:
            print("Breakpoint number not specified")
            return False

        try:
            number = int(arg)
        except ValueError:
            print("Invalid breakpoint number")
            return False

        self._delete_breakpoint(number)

    def _delete_breakpoint(self, number: int) -> None:
        for path, filebps in self._breakpoints.items():
            for line, bp in filebps.items():
                if bp.number == number:
                    del filebps[line]
                    if not filebps:
                        del self._breakpoints[path]
                    return
        print(f"Invalid breakpoint number")

    def do_stack(self, _) -> bool:
        frame = self._frame
        while frame and frame is not self._first_frame.f_back:
            func = frame.f_code.co_name
            file = frame.f_code.co_filename
            line = frame.f_lineno
            print(f"{hex(id(frame))} in {func} at {file}:{line}")
            frame = frame.f_back
        return False

    def do_exit(self, _) -> bool:
        if self._running:
            raise DebugExit()
        self._exit = True
        return True

    def postloop(self) -> None:
        if not self._exit and not self._running:
            try:
                self._running = True
                globals = {"__name__": "__main__", "__builtins__": __builtins__}
                sys.settrace(self._handle_trace_event)
                code = compile(self.path.read_text(), self.path.resolve(), "exec")
                exec(code, globals)
                sys.settrace(None)  # Prevent debugger tracing itself
                print("Execution completed")
            except DebugExit:
                self._exit = True
            finally:
                sys.settrace(None)
                self._running = False
                self._first_frame = None

            self.start_prompt()

    def _handle_trace_event(self, frame: FrameType, event: str, _):
        with self._save_frame(frame):
            match event:
                case "call":
                    return self._handle_call()
                case "line":
                    return self._handle_line()
                case "return":
                    return self._handle_return()
        return None

    @contextmanager
    def _save_frame(self, frame: FrameType):
        self._frame = frame
        try:
            yield
        finally:
            self._frame = None

    def _handle_call(self):
        trace_handler = None

        path, _ = self._get_source_location(self._frame)
        if self._first_frame is None and self.path == path:
            self._first_frame = self._frame

        if self._has_file_breakpoint(self._frame) or self._should_step_into():
            trace_handler = self._handle_trace_event

        if self._func_breakpoint_hit(self._frame):
            self._break(self._frame)

        return trace_handler

    def _handle_line(self):
        if self._should_step_into():
            self._step_into()
        elif self._should_step_over():
            self._step_over()
        elif self._should_step_out():
            self._step_out()
        elif self._line_breakpoint_hit(self._frame):
            self._break(self._frame)
        return self._handle_trace_event

    def _handle_return(self):
        pass

    def _has_file_breakpoint(self, frame: FrameType) -> bool:
        file, _ = self._get_source_location(frame)
        return file in self._breakpoints

    def _func_breakpoint_hit(self, frame: FrameType) -> bool:
        path, _ = self._get_source_location(frame)

        filebps = self._breakpoints.get(path)
        if not filebps:
            return False

        func = self._frame.f_code.co_name

        bp = filebps.get(func)
        if not bp:
            return False

        if bp.temp:
            print(f"Removing temporary breakpoint")
            self._delete_breakpoint(bp.number)

        return True

    def _line_breakpoint_hit(self, frame: FrameType) -> bool:
        path, line = self._get_source_location(frame)

        file_bps = self._breakpoints.get(path)
        if not file_bps:
            return False

        bp = file_bps.get(line)
        if not bp:
            return False

        if bp.temp:
            print(f"Removing temporary breakpoint")
            self._delete_breakpoint(bp.number)

        return True

    def _should_step_into(self) -> bool:
        return self._step_mode == StepMode.STEP_INTO

    def _should_step_over(self) -> bool:
        step_over = self._step_mode == StepMode.STEP_OVER
        return step_over and self._frame is self._start_frame

    def _should_step_out(self) -> bool:
        step_out = self._step_mode == StepMode.STEP_OUT
        return step_out and self._frame is self._end_frame

    def _step_into(self) -> None:
        self._step_mode = StepMode.NONE
        self._break(self._frame)

    def _step_over(self) -> None:
        self._step_mode = StepMode.NONE
        self._start_frame = None
        self._break(self._frame)

    def _step_out(self) -> None:
        self._step_mode = StepMode.NONE
        self._end_frame = None
        self._break(self._frame)

    def _break(self, frame: FrameType) -> None:
        path, line = self._get_source_location(frame)
        self._show_lines(path, line, count=1)
        self.start_prompt()

    def _show_lines(self, path: Path, line: int, count: int) -> None:
        lines = path.read_text().splitlines(keepends=True)
        if count == 1:
            print(f"{line}: {lines[line - 1]}", end="")
        else:
            start = max(0, line - count // 2)
            end = min(len(lines), line + (count // 2) - 1)
            width = len(str(end))
            for i in range(start, end):
                if i == line - 1:
                    print(f"-> {i+1:{width}d}: {lines[i]}", end="")
                else:
                    print(f"   {i+1:{width}d}: {lines[i]}", end="")

    def _get_source_location(self, frame: FrameType):
        line = frame.f_lineno
        try:
            path = Path(frame.f_code.co_filename).resolve(strict=True)
        except:
            return None, line
        return path, line


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve(strict=True) if len(sys.argv) > 1 else None

    if path:
        if not path.exists():
            print(f'File "{path}" not found')
            sys.exit(1)

        if not path.is_file():
            print(f'Path "{path}" is not a file')
            sys.exit(1)

    debug = Debug(path)
    try:
        debug.start_prompt()
    except (KeyboardInterrupt, DebugExit):
        pass
