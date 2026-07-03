import cmd
import enum
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Dict, Set


@dataclass
class Breakpoint:
    temp: bool = False


class StepMode(enum.Enum):
    NONE = 0
    STEP_INTO = 1
    STEP_OVER = 2
    STEP_OUT = 3


class DebugExit(Exception):
    pass


class Debug(cmd.Cmd):
    prompt = "> "

    def __init__(self, path: Path = None) -> None:
        super().__init__()
        self.path = path
        self._exit = False
        self._running = False
        self._breakpoints: Dict[Path, Dict[int, Breakpoint]] = {}
        self._step_mode = StepMode.NONE
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
            filename, line = self._parse_breakpoint_location(location)
            self._add_breakpoint(filename, line, False)
        except ValueError as e:
            print(e)
        return False

    def do_tbreak(self, location: str) -> bool:
        try:
            filename, line = self._parse_breakpoint_location(location)
            self._add_breakpoint(filename, line, True)
        except ValueError as e:
            print(e)
        return False

    def _parse_breakpoint_location(self, location: str):
        PATTERN = r"(?P<filename>.+):(?P<line>\d+)"
        if m := re.fullmatch(PATTERN, location):
            return m.group("filename"), m.group("line")
        raise ValueError("Invalid breakpoint format")

    def _add_breakpoint(self, filename: str, line: str, temp: bool) -> None:
        try:
            path = Path(filename).resolve(strict=True)
            lineno = int(line)
            if lineno <= 0:
                print("Line number must be greater than or equal to one")
                return

            file_bps = self._breakpoints.get(path, None)
            if file_bps:
                file_bps[lineno] = Breakpoint(temp)
            else:
                self._breakpoints[path] = {lineno: Breakpoint(temp)}
        except OSError:
            print(f"Failed to resolve file path")

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
        if self._frame.f_back:
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
        if len(self._breakpoints) > 0:
            for path, breakpoints in self._breakpoints.items():
                for line, breakpoint in breakpoints.items():
                    message = f"{path}:{line}"
                    if breakpoint.temp:
                        message += "*"
                    print(message)
        else:
            print("No breakpoints specified")

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
        if self._has_file_breakpoint(self._frame) or self._should_step_into():
            return self._handle_trace_event
        return None

    def _handle_line(self):
        if self._should_step_into():
            self._step_into()
        elif self._should_step_over():
            self._step_over()
        elif self._should_step_out():
            self._step_out()
        elif self._breakpoint_hit(self._frame):
            self._break(self._frame)
        return self._handle_trace_event

    def _handle_return(self):
        pass

    def _has_file_breakpoint(self, frame: FrameType) -> bool:
        file, _ = self._get_source_location(frame)
        return file in self._breakpoints

    def _breakpoint_hit(self, frame: FrameType) -> bool:
        path, line = self._get_source_location(frame)

        file_bps = self._breakpoints.get(path)
        if not file_bps:
            return False

        bp = file_bps.get(line)
        if not bp:
            return False

        if bp.temp:
            print(f"Removing temporary breakpoint")
            del file_bps[line]

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
            path = Path(frame.f_code.co_filename).resolve()
        except:
            return None, line
        return path, line


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None

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
