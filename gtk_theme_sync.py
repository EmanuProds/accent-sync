#!/usr/bin/env python3
"""Sincroniza o tema GTK e o accent do GNOME com Zed, Ghostty e Flatpak.

Le o color-scheme e accent-color em org.gnome.desktop.interface e:
- regrava as chaves de accent do tema do Zed (zedwaita.json)
- regrava a chave `background` dos temas do Ghostty
- define gtk-theme (adw-gtk3/adw-gtk3-dark) e settings.ini GTK3/4
- copia temas GTK3 para ~/.local/share/themes (Flatpak)

O tint e sempre recalculado a partir de bases neutras fixas, nunca do valor
ja gravado, entao o script e idempotente: rodar mil vezes da o mesmo resultado
e trocar de cor nao acumula tom.

Uso:
  gtk-theme-sync            regrava os arquivos
  gtk-theme-sync --dry-run  so mostra o que mudaria
"""

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

HOME = pathlib.Path.home()
ZED_THEME = HOME / ".config/zed/themes/zedwaita.json"
GHOSTTY_THEME_DIR = HOME / ".config/ghostty/themes"

# Accents do GNOME: (accent_bg_color, variante clareada para texto em dark).
# A primeira e a cor base, a que o Nautilus e os demais apps GTK usam; a
# segunda e o que o libadwaita usa para texto sobre fundo escuro.
ACCENTS = {
    "blue": ("#3584e4", "#81d0ff"),
    "teal": ("#2190a4", "#7bdff4"),
    "green": ("#3a944a", "#8de698"),
    "yellow": ("#c88800", "#ffc057"),
    "orange": ("#ed5b00", "#ff9c5b"),
    "red": ("#e62d42", "#ff888c"),
    "pink": ("#d56199", "#ffa0d8"),
    "purple": ("#9141ac", "#fba7ff"),
    "slate": ("#6f8396", "#bbd1e5"),
}

# --- Zed: chaves de accent puro -------------------------------------------------
ZED_PLAIN = [
    "text.accent", "icon.accent", "link_text.hover",
    "border.focused", "border.selected",
    "pane.focused_border", "panel.focused_border",
    "info", "renamed",
]

# --- Zed: chaves de accent com alpha fixo --------------------------------------
ZED_ALPHA = {
    "info.background": "1a", "info.border": "15",
    "renamed.background": "1a", "renamed.border": "15",
    "hint.border": "15",
    "drop_target.background": "80",
    "search.match_background": "40",
    "version_control.conflict_marker.theirs": "1a",
    "editor.document_highlight.read_background": "15",
}

# --- Zed: estados de selecao ---------------------------------------------------
# Usa o accent BASE (como o Nautilus), nao a variante de texto: com a variante
# de texto a selecao fica num cinza-arroxeado sem relacao com o resto do
# desktop. 35% no active, 22% no selected (para manter hierarquia).
ZED_SELECTION = {
    "element.active": "59", "element.selected": "38",
    "ghost_element.active": "59", "ghost_element.selected": "38",
}

# --- Zed: superficies, sem tint -----------------------------------------------
# Bases medidas no Nautilus com o CSS real do sistema (accent slate, sem
# ChromaLeon): sidebar #2e2e32, area de conteudo #1d1d20, chrome #222226.
# Sao exatamente as cores que a paleta Adwaita define (sidebar_bg_color,
# view_bg_color e window_bg_color), entao o Zed fica com o mesmo tom dos apps
# GTK4 do sistema em vez de mais claro.
#
# Sem tint de proposito: o adw-gtk3-colorizer mexe so no accent, e um tint de
# accent sobre surface escura clareia tudo, deixando o Zed ~5 pontos acima dos
# demais apps.
ZED_SURFACES = {
    # chrome / fundo da janela
    "background": ("#222226", "#fafafa"),
    "tab_bar.background": ("#222226", "#fafafa"),
    "tab.inactive_background": ("#222226", "#fafafa"),
    "title_bar.background": ("#222226", "#ffffff"),
    "title_bar.inactive_background": ("#222226", "#ebebeb"),
    "terminal.background": ("#222226", "#fcfcfc"),
    # meio nivel: sidebar, paineis e superficies elevadas
    "surface.background": ("#2e2e32", "#ffffff"),
    "elevated_surface.background": ("#2e2e32", "#ffffff"),
    "panel.background": ("#2e2e32", "#ebebeb"),
    "toolbar.background": ("#2e2e32", "#ffffff"),
    "status_bar.background": ("#2e2e32", "#ebebeb"),
    "tab.active_background": ("#2e2e32", "#ffffff"),
    "editor.subheader.background": ("#2e2e32", "#ebebeb"),
    "editor.active_line.background": ("#2e2e32", "#f6f5f4"),
    # nivel mais escuro: o editor
    "editor.background": ("#1d1d20", "#fcfcfc"),
    "editor.gutter.background": ("#1d1d20", "#fcfcfc"),
}

# --- Ghostty: base do background e intensidade do tint -------------------------
GHOSTTY_BACKGROUND = {
    "dark": ("#1d1d20", 0.05),
    "light": ("#ffffff", 0.07),
}

# Cursor e selecao de texto ficam neutros de proposito (como no Zed): o accent
# nao deve colorir o conteudo do usuario.


def gsettings(key):
    """Le uma chave do dconf e devolve o valor sem aspas."""
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", key],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    if len(out) >= 2 and out[0] == out[-1] == "'":
        return out[1:-1]
    return out


def mix_hex(base, accent, ratio):
    """Mistura `accent` sobre `base` na proporcao `ratio` (0-1)."""
    def parse(value):
        h = value.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) > 6:
            h = h[:6]
        if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
            return None
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]

    b, a = parse(base), parse(accent)
    if b is None or a is None:
        return base
    return "#" + "".join(
        f"{round(bi + (ai - bi) * ratio):02x}" for bi, ai in zip(b, a)
    )


def resolve_accent():
    """Devolve (base, texto) do accent atual, ou None se nao recognitionavel."""
    name = gsettings("accent-color")
    if name in ACCENTS:
        return ACCENTS[name]
    #GNOME 47+ aceita hex direto; nesse caso base e texto sao a mesma cor
    if re.fullmatch(r"#[0-9a-fA-F]{6}", name):
        return name, name
    return None


def escape(key):
    return key.replace(".", r"\.")


def rewrite(content, key, alpha=None, first=None):
    """Substitui o valor de `key` em todas as ocorrencias.

    `alpha` fixa o sufixo de transparencia. `first` permite usar cores
    diferentes na 1a ocorrencia (tema dark) e nas seguintes (tema light).
    """
    suffix = f"([0-9a-fA-F]{{2}})?" if alpha else ""
    pattern = re.compile(rf'("{escape(key)}":\s*")#[0-9a-fA-F]{{6}}{suffix}"')
    count = 0

    def replace(match):
        nonlocal count
        count += 1
        return f'{match.group(1)}{first(count)}{alpha or ""}"'

    return pattern.sub(replace, content), count


def sync_zed(base_hex, text_hex, dry_run=False):
    if not ZED_THEME.exists():
        print(f"zed: {ZED_THEME} nao existe, pulando")
        return

    content = ZED_THEME.read_text(encoding="utf-8")
    original = content

    # accent puro: dark usa a variante de texto, light usa a base
    for key in ZED_PLAIN:
        content, _ = rewrite(
            content, key, first=lambda n: text_hex if n == 1 else base_hex
        )
    # accent com alpha: mesma regra de cor, alpha fixo
    for key, alpha in ZED_ALPHA.items():
        content, _ = rewrite(
            content, key, alpha=alpha,
            first=lambda n: text_hex if n == 1 else base_hex,
        )
    # (as chaves acima usam a variante de texto no tema dark, porque ali o
    # accent precisa de contraste sobre fundo escuro)
    # estados de selecao: sempre o accent base
    for key, alpha in ZED_SELECTION.items():
        content, _ = rewrite(
            content, key, alpha=alpha, first=lambda n: base_hex
        )
    # superficies: base pura, sem tint (para casar com os apps GTK4 do sistema)
    for key, (dark_base, light_base) in ZED_SURFACES.items():
        pattern = re.compile(rf'("{escape(key)}":\s*")#[0-9a-fA-F]{{6}}([0-9a-fA-F]{{2}})?"')
        counter = 0

        def surface(match, _dark=dark_base, _light=light_base):
            nonlocal counter
            counter += 1
            return f'{match.group(1)}{_dark if counter == 1 else _light}"'

        content = pattern.sub(surface, content)

    if content == original:
        print("zed: ja estava em dia")
        return
    if dry_run:
        print("zed: (dry-run) mudaria")
        return
    ZED_THEME.write_text(content, encoding="utf-8")
    print(f"zed: tema atualizado (accent base {base_hex}, texto {text_hex})")


def sync_ghostty(base_hex, dry_run=False):
    for appearance, (bg_base, ratio) in GHOSTTY_BACKGROUND.items():
        path = GHOSTTY_THEME_DIR / f"gtk-theme-sync-{appearance}"
        if not path.exists():
            print(f"ghostty: {path} nao existe, pulando")
            continue
        content = path.read_text(encoding="utf-8")
        tinted = mix_hex(bg_base, base_hex, ratio)
        updated = re.sub(
            r"^background = #[0-9a-fA-F]{6}$",
            f"background = {tinted}",
            content,
            flags=re.MULTILINE,
        )
        if updated == content:
            print(f"ghostty/{appearance}: ja estava em dia")
            continue
        if dry_run:
            print(f"ghostty/{appearance}: (dry-run) mudaria")
            continue
        path.write_text(updated, encoding="utf-8")
        print(f"ghostty/{appearance}: background -> {tinted}")


def sync_flatpak_themes():
    """Copia os temas GTK3 do sistema para ~/.local/share/themes.

    Apps Flatpak nao enxergam /usr/share/themes (o Flatpak monta o host em
    /run/host, fora do XDG_DATA_DIRS do app) e um symlink tambem nao resolve,
    porque o alvo fica fora do sandbox. A unica via que funciona e ter uma copia
    real dentro de um diretorio concedido.

    Recopia so quando a assinatura do tema de origem muda, ou seja, quando o
    pacote do sistema e atualizado.
    """
    src_root = pathlib.Path("/usr/share/themes")
    dst_root = HOME / ".local/share/themes"
    stamp_file = HOME / ".cache/gtk-theme-sync/themes.stamp"
    themes = ("adw-gtk3", "adw-gtk3-dark")

    signatures = {}
    for name in themes:
        source = src_root / name
        if not source.is_dir():
            print(f"flatpak: {source} ausente, pulando")
            return
        digest = hashlib.sha256()
        for item in sorted(source.rglob("*")):
            if item.is_file():
                digest.update(str(item.relative_to(source)).encode())
                digest.update(str(item.stat().st_size).encode())
        signatures[name] = digest.hexdigest()

    previous = {}
    if stamp_file.exists():
        for line in stamp_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                previous[key] = value

    if previous == signatures:
        return

    for name in themes:
        destination = dst_root / name
        temporary = dst_root / f".{name}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(src_root / name, temporary)
        if destination.exists():
            shutil.rmtree(destination)
        temporary.rename(destination)

    stamp_file.parent.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(
        "".join(f"{k}={v}\n" for k, v in sorted(signatures.items())),
        encoding="utf-8",
    )
    print("flatpak: temas GTK3 copiados para ~/.local/share/themes")


def gsettings_set(key, value):
    """Grava uma chave do dconf; devolve True em sucesso."""
    try:
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.interface", key, value],
            check=True, timeout=5,
        )
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"gsettings: falha ao gravar {key}: {exc}", file=sys.stderr)
        return False


def gtk_theme_for_scheme(scheme):
    """Mapeia color-scheme do GNOME para o tema GTK3 legado correspondente."""
    return "adw-gtk3-dark" if "dark" in (scheme or "") else "adw-gtk3"


def update_settings_ini(path, theme, prefer_dark):
    """Garante gtk-theme-name e gtk-application-prefer-dark-theme no settings.ini."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    # localiza ou cria a secao [Settings]
    section_index = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "[settings]":
            section_index = i
            break
    if section_index is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[Settings]")
        section_index = len(lines) - 1

    # coleta chaves existentes na secao (ate a proxima secao ou fim)
    end = len(lines)
    for j in range(section_index + 1, len(lines)):
        if lines[j].lstrip().startswith("[") and lines[j].rstrip().endswith("]"):
            end = j
            break

    wanted = {
        "gtk-theme-name": theme,
        "gtk-application-prefer-dark-theme": "1" if prefer_dark else "0",
    }
    present = set()
    for j in range(section_index + 1, end):
        if "=" in lines[j]:
            key = lines[j].split("=", 1)[0].strip()
            if key in wanted:
                lines[j] = f"{key} = {wanted[key]}"
                present.add(key)

    # insere as que faltam logo apos o cabecalho da secao
    insert_at = section_index + 1
    for key in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
        if key not in present:
            lines.insert(insert_at, f"{key} = {wanted[key]}")
            insert_at += 1
            end += 1

    new_content = "\n".join(lines) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == new_content:
        return False
    path.write_text(new_content, encoding="utf-8")
    return True


def sync_gtk_theme(dry_run=False):
    """Sincroniza gtk-theme (apps legados) com o color-scheme do GNOME.

    O GNOME 50 nao tem mais Adwaita-dark em /usr/share/themes, entao um
    gtk-theme 'Adwaita-dark' (valor antigo) cai em HighContrast. Aqui forçamos
    o par adw-gtk3/adw-gtk3-dark, que e o que o gtk-theme-sync copia para Flatpak,
    e mantemos o settings.ini dos apps GTK3/GTK4 em dia (bug do GNOME 50 em
    que GTK4 ignora prefer-dark via gsettings).
    """
    scheme = gsettings("color-scheme")
    target = gtk_theme_for_scheme(scheme)
    dark = "dark" in (scheme or "")
    current = gsettings("gtk-theme")

    changed = False
    if current != target:
        if dry_run:
            print(f"gtk-theme: (dry-run) {current!r} -> {target!r}")
        else:
            if gsettings_set("gtk-theme", target):
                print(f"gtk-theme: {current!r} -> {target!r}")
                changed = True
            else:
                print(f"gtk-theme: nao foi possivel gravar (atual {current!r})")
    else:
        print(f"gtk-theme: ja estava em {target!r}")

    for path in (HOME / ".config/gtk-3.0/settings.ini",
                 HOME / ".config/gtk-4.0/settings.ini"):
        if dry_run:
            desired = f"[Settings]\ngtk-theme-name = {target}\ngtk-application-prefer-dark-theme = {'1' if dark else '0'}\n"
            try:
                current_content = path.read_text(encoding="utf-8") if path.exists() else ""
            except OSError:
                current_content = ""
            if current_content == desired:
                print(f"settings.ini: {path} ja estava em dia")
            else:
                print(f"settings.ini: (dry-run) atualizaria {path}")
            continue
        if update_settings_ini(path, target, dark):
            print(f"settings.ini: {path} atualizado")
            changed = True
        else:
            print(f"settings.ini: {path} ja estava em dia")
    return changed


def main():
    dry_run = "--dry-run" in sys.argv
    accent = resolve_accent()
    if accent is None:
        name = gsettings("accent-color") or "?"
        print(f"accent '{name}' nao reconhecido; nada alterado", file=sys.stderr)
        return 1
    base_hex, text_hex = accent
    if dry_run:
        print(f"-- dry-run: accent base {base_hex}, texto {text_hex} --")
    sync_zed(base_hex, text_hex, dry_run)
    sync_ghostty(base_hex, dry_run)
    sync_gtk_theme(dry_run)
    if not dry_run:
        sync_flatpak_themes()
    return 0


if __name__ == "__main__":
    sys.exit(main())
