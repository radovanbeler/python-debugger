import cmd
import enum
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Dict, Set


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
        self._breakpoints: Dict[Path, Set[int]] = {}
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
        PATTERN = r"(?P<filename>.+):(?P<line>\d+)"
        if m := re.fullmatch(PATTERN, location):
            self._add_breakpoint(m.group("filename"), m.group("line"))
        else:
            print("Invalid breakpoint format")
        return False

    def _add_breakpoint(self, filename: str, line: str) -> None:
        path = Path(filename)
        if path.exists():
            path = path.resolve()
            lineno = int(line)
            if lineno >= 1:
                self._breakpoints[path] = set([lineno])
            else:
                print("Line number must be greater than or equal to one")
        else:
            print("File not found")

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
        elif self._has_line_breakpoint(self._frame):
            self._break(self._frame)
        return self._handle_trace_event

    def _handle_return(self):
        pass

    def _has_file_breakpoint(self, frame: FrameType) -> bool:
        file, _ = self._get_source_location(frame)
        return file in self._breakpoints

    def _has_line_breakpoint(self, frame: FrameType) -> bool:
        file, line = self._get_source_location(frame)
        if file in self._breakpoints:
            return line in self._breakpoints[file]
        return False

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
