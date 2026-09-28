import sys
import time
from datetime import date


def _clear_console():
    """Limpia la consola COMPLETA (pantalla + scrollback/historial) usando la
    API nativa de Windows. En cmd clásico las secuencias ESC[3J/2J no borran
    el scrollback; rellenar todo el buffer con espacios + home sí lo hace"""
    if not sys.stdout.isatty():
        return
    if sys.platform != "win32":
        sys.stdout.write("\033[3J\033[2J\033[H")
        return
    try:
        import ctypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [
                ("Left", ctypes.c_short),
                ("Top", ctypes.c_short),
                ("Right", ctypes.c_short),
                ("Bottom", ctypes.c_short),
            ]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [
                ("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", ctypes.c_ushort),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD),
            ]

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        csbi = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(csbi)):
            sys.stdout.write("\033[3J\033[2J\033[H")
            return
        n_cells = csbi.dwSize.X * csbi.dwSize.Y
        written = ctypes.c_ulong()
        coord = COORD(0, 0)
        kernel32.FillConsoleOutputCharacterW(
            handle, ctypes.c_wchar(" "), n_cells, coord, ctypes.byref(written)
        )
        kernel32.FillConsoleOutputAttribute(
            handle, csbi.wAttributes, n_cells, coord, ctypes.byref(written)
        )
        kernel32.SetConsoleCursorPosition(handle, coord)
    except Exception:
        sys.stdout.write("\033[3J\033[2J\033[H")


class ImportStats:
    def __init__(self):
        self.tickets_added = 0
        self.tickets_updated = 0
        self.tickets_unchanged = 0
        self.conv_added = 0
        self.conv_updated = 0
        self.conv_unchanged = 0
        self.errors = 0
        self.error_tickets = []
        self.no_existen = 0
        self.ultimo_ticket = 0
        self.categorias_added = 0
        self.categorias_updated = 0
        self.categorias_unchanged = 0
        self.estados_updated = 0
        self.llamadas_added = 0
        self.tickets_embedding_generado = 0
        self.tickets_matcheado = 0
        self.matches = []
        self.tiempo_inicio = time.time()
        self.status = ""

    def _error_str(self):
        if not self.error_tickets:
            return "0"
        ids = ", ".join(str(t) for t in self.error_tickets[:5])
        if len(self.error_tickets) > 5:
            ids += f" ... +{len(self.error_tickets) - 5}"
        return f"{len(self.error_tickets)} ({ids})"

    def _tiempo_str(self):
        elapsed = int(time.time() - self.tiempo_inicio)
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d} HS"

    def _build_lines(self):
        lines = []
        lines.append(f"{'=' * 36}")
        lines.append("{:<20}: {}".format("ULTIMO TICKET", self.ultimo_ticket if self.ultimo_ticket else "-"))
        lines.append("{:<20}: {}".format("FECHA UPDATE", date.today().strftime("%d/%m/%Y")))
        lines.append(f"{'=' * 36}")
        lines.append("{:<20}: {}".format("TICKETS AGREGADOS", self.tickets_added))
        lines.append("{:<20}: {}".format("TICKETS ACTUALIZADOS", self.tickets_updated))
        lines.append("{:<20}: {}".format("TICKETS IGUALES", self.tickets_unchanged))
        lines.append("{:<20}: {}".format("TICKETS NO EXISTEN", self.no_existen))
        lines.append("")
        lines.append("{:<20}: {}".format("CONV AGREGADAS", self.conv_added))
        lines.append("{:<20}: {}".format("CONV ACTUALIZADAS", self.conv_updated))
        lines.append("{:<20}: {}".format("CONV IGUALES", self.conv_unchanged))
        lines.append("")
        lines.append("{:<20}: {}".format("ERRORES", self._error_str()))
        lines.append(f"{'=' * 36}")
        return lines

    def _build_pipeline_lines(self):
        lines = []
        if self.status:
            lines.append(self.status)
        lines.append(f"{'=' * 40}")
        lines.append("{:<33}: {}".format("ULTIMO TICKET", self.ultimo_ticket if self.ultimo_ticket else "-"))
        lines.append("{:<33}: {}".format("TIEMPO DE EJECUCION", self._tiempo_str()))
        lines.append(f"{'=' * 40}")
        lines.append("{:<33}: {}".format("TICKETS AGREGADOS", self.tickets_added))
        lines.append("{:<33}: {}".format("TICKETS ACTUALIZADOS", self.tickets_updated))
        lines.append("{:<33}: {}".format("TICKETS EMBEDDING GEN", self.tickets_embedding_generado))
        lines.append("{:<33}: {}".format("TICKETS MATCHEADO", self.tickets_matcheado))
        for t_id, p_id, p_sim, p_problema in self.matches:
            lines.append("  Ticket {:<8} -> Patron {:<4} (sim {:.4f})  {}".format(
                t_id, p_id, p_sim, p_problema or ""))
        lines.append("")
        lines.append("{:<33}: {}".format("CONVERSACIONES AGREGADAS", self.conv_added))
        lines.append("")
        lines.append("{:<33}: {}".format("CATEGORIAS AGREGADAS", self.categorias_added))
        lines.append("{:<33}: {}".format("CATEGORIAS ACTUALIZADAS", self.categorias_updated))
        lines.append("")
        lines.append("{:<33}: {}".format("ESTADOS ACTUALIZADOS", self.estados_updated))
        lines.append("")
        lines.append("{:<33}: {}".format("LLAMADAS AGREGADAS", self.llamadas_added))
        lines.append("")
        lines.append("{:<33}: {}".format("ERRORES", self._error_str()))
        lines.append(f"{'=' * 40}")
        return lines

    def print_slim(self):
        lines = self._build_lines()
        text = "\n".join(lines)
        if sys.stdout.isatty():
            _clear_console()
            sys.stdout.write(text)
        else:
            sys.stdout.write(f"\n{text}\n")
        sys.stdout.flush()

    def print_pipeline(self, extra_lines=None):
        lines = self._build_pipeline_lines()
        if extra_lines:
            lines.extend(extra_lines)
        text = "\n".join(lines)
        if sys.stdout.isatty():
            _clear_console()
            sys.stdout.write(text)
        else:
            sys.stdout.write(f"\n{text}\n")
        sys.stdout.flush()
        self._ultimo_print = time.time()

    def print_pipeline_final(self, extra_lines=None):
        """Print de cierre. Si recibe `extra_lines` (historial, etc.) SIEMPRE
        redibuja el tablero completo en un solo frame: tablero + extra juntos,
        para que nada pueda quedar tapado por el clear previo."""
        if extra_lines:
            self.print_pipeline(extra_lines=extra_lines)
            return
        if time.time() - getattr(self, "_ultimo_print", 0) < 1:
            return
        self.print_pipeline()

    def print_summary(self):
        lines = self._build_lines()
        print(f"\n{'=' * 32}")
        print("RESUMEN")
        print("=" * 32)
        print(f"TICKETS AGREGADOS    : {self.tickets_added}")
        print(f"TICKETS ACTUALIZADOS : {self.tickets_updated}")
        print(f"TICKETS IGUALES      : {self.tickets_unchanged}")
        print(f"TICKETS NO EXISTEN   : {self.no_existen}")
        print()
        print(f"CONV AGREGADAS       : {self.conv_added}")
        print(f"CONV ACTUALIZADAS    : {self.conv_updated}")
        print(f"CONV IGUALES         : {self.conv_unchanged}")
        print()
        print(f"ERRORES              : {self._error_str()}")
        print("=" * 32)
