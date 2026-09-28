"""Utilidades de línea de comandos.

    python -m vpnxgb.cli hash-password      # pide una contraseña y muestra su hash
    python -m vpnxgb.cli collect            # fuerza una lectura de consumo
"""

import getpass
import sys

from .auth import hash_password


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "hash-password":
        if not sys.stdin.isatty():
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Contraseña: ")
            if password != getpass.getpass("Repite la contraseña: "):
                print("Las contraseñas no coinciden", file=sys.stderr)
                return 1
        print(hash_password(password))
        return 0
    if cmd == "collect":
        from .app import build_panel

        build_panel().collect_usage()
        print("Consumo actualizado")
        return 0
    print(f"Comando desconocido: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
