#!/usr/bin/env python3
"""
Agent d'impression (modèle PULL / long polling).

Tourne sur un poste client. Boucle :
  1. POLL le serveur (long polling ~30s) → récupère un job (étiquette(s) en
     image PNG, déjà entièrement composées côté serveur, + imprimantes)
  2. IMPRIME chaque image sur la première imprimante joignable, via le
     pilote Windows (GDI) — l'imprimante ne reçoit que des pixels, quel que
     soit son firmware (fini les limites de police/langue du ZPL)
  3. ACK le serveur (succès + imprimante utilisée, ou échec + raison)
  4. Recommence

Connexion SORTANTE uniquement (compatible NAT/pare-feu d'entreprise) :
le serveur n'initie jamais rien vers l'agent.

Toutes les imprimantes (réseau ou USB) doivent être installées comme
imprimante Windows classique (pilote du fabricant) sur ce poste : l'agent
imprime toujours via son nom Windows exact (champ 'windows_printer_name'
de l'admin Django) — utiliser --list-printers pour connaître ce nom.

Config : fichier agent.ini par défaut, surchargeable en CLI.
    python agent.py                          # utilise agent.ini
    python agent.py --server https://... --token XXX
    python agent.py --list-printers          # affiche les imprimantes Windows
    python agent.py --test-print-image etiquette.png --test-printer "Zebra ZD220"

Dépendances : requests, pywin32, Pillow (pip install -r requirements.txt)
"""
from __future__ import annotations
import argparse
import base64
import configparser
import logging
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

try:
    import win32con
    import win32gui
    import win32print
    import win32ui
    from PIL import Image, ImageWin
    HAS_WIN32PRINT = True
except ImportError:
    HAS_WIN32PRINT = False

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENT] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Délai d'attente réseau avant de réessayer un poll quand le serveur est
# injoignable (erreur réseau, VPS down). Évite de marteler en boucle serrée.
NETWORK_RETRY_DELAY = 5

# Timeout de la requête de poll : doit être > au long-poll serveur (30s) pour
# laisser le serveur tenir la connexion ouverte. On met une marge.
POLL_HTTP_TIMEOUT = 45

# Délai entre deux étiquettes d'un même job (ex: étiquette double, ou
# label_qty > 1) envoyées à la suite à la même imprimante. Sans ça, deux
# StartDoc/EndDoc GDI trop rapprochés peuvent arriver avant que l'imprimante
# thermique ait fini de faire avancer/couper l'étiquette précédente : le
# contenu suivant se retrouve alors imprimé par-dessus, sur la même
# étiquette physique, au lieu d'une nouvelle (repéré au tirage réel : 2
# étiquettes superposées et illisibles au lieu de 2 étiquettes distinctes).
PRINT_JOB_DELAY = 1.5


def list_windows_printers() -> None:
    """
    Affiche les imprimantes installées dans Windows avec leur nom exact.
    Utile au déploiement : le nom affiché ici est celui à saisir dans
    le champ 'windows_printer_name' de l'admin Django.
    """
    if not HAS_WIN32PRINT:
        print("win32print indisponible (pywin32 non installé ou hors Windows).")
        return
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printers = win32print.EnumPrinters(flags, None, 1)
    print("Imprimantes installées sur ce poste :")
    for p in printers:
        # p[2] = nom de l'imprimante tel que Windows l'expose
        print(f"  - {p[2]}")


def load_config(config_path: str) -> dict:
    """Charge la config depuis un fichier .ini (section [agent])."""
    cfg = {"server": "", "token": ""}
    path = Path(config_path)
    if path.exists():
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        if parser.has_section("agent"):
            cfg["server"] = parser.get("agent", "server", fallback="")
            cfg["token"] = parser.get("agent", "token", fallback="")
    return cfg


def _dump_devmode(devmode) -> None:
    for field in ("DeviceName", "FormName", "PaperSize", "PaperWidth", "PaperLength", "Orientation", "Fields"):
        print(f"  {field}: {getattr(devmode, field, '<absent>')}")


def print_devmode_info(printer_name: str) -> None:
    """
    Diagnostic : affiche le DEVMODE réellement utilisé pour cette
    imprimante, avant et après DocumentProperties, plus la taille de zone
    imprimable (HORZRES/VERTRES) qui en résulte. Sert à comprendre pourquoi
    une étiquette sort mal formatée (mauvaise taille/forme/orientation)
    sans avoir à deviner à l'aveugle à chaque essai.
    """
    if not HAS_WIN32PRINT:
        print("pywin32 indisponible.")
        return

    handle = win32print.OpenPrinter(printer_name)
    try:
        devmode_before = win32print.GetPrinter(handle, 2)["pDevMode"]
        print("--- DEVMODE (GetPrinter, AVANT DocumentProperties) ---")
        _dump_devmode(devmode_before)

        devmode_after = win32print.GetPrinter(handle, 2)["pDevMode"]
        win32print.DocumentProperties(
            0, handle, printer_name, devmode_after, devmode_after,
            win32con.DM_OUT_BUFFER | win32con.DM_IN_BUFFER,
        )
        print("--- DEVMODE (APRÈS DocumentProperties IN+OUT) ---")
        _dump_devmode(devmode_after)

        raw_hdc = win32gui.CreateDC("WINSPOOL", printer_name, devmode_after)
        dc = win32ui.CreateDCFromHandle(raw_hdc)
        try:
            width = dc.GetDeviceCaps(win32con.HORZRES)
            height = dc.GetDeviceCaps(win32con.VERTRES)
            print(f"--- DC résultant : HORZRES={width} VERTRES={height} ---")
        finally:
            dc.DeleteDC()
    finally:
        win32print.ClosePrinter(handle)


def _print_image_gdi(
    image: "Image.Image",
    printer_name: str,
    label_width_mm: float | None = None,
    label_height_mm: float | None = None,
) -> None:
    """
    Imprime une image PIL sur `printer_name` via le pilote Windows (GDI).

    Remplace l'ancien envoi ZPL brut (socket TCP ou spouleur en mode RAW) :
    l'image est déjà entièrement composée (texte, logos, code-barres) côté
    serveur, donc l'imprimante n'a plus besoin d'interpréter quoi que ce
    soit ni de posséder la moindre police — elle imprime des pixels, comme
    une photo.

    L'image est étirée pour remplir toute la zone imprimable déclarée par
    le pilote (HORZRES/VERTRES) : la taille physique réelle de l'étiquette
    est donc celle configurée dans les propriétés du pilote Windows (format
    de papier/étiquette), pas dans ce code — sauf si `label_width_mm`/
    `label_height_mm` sont fournis (cf. plus bas).

    Important : on récupère explicitement le DEVMODE via DocumentProperties
    (l'API derrière la boîte de dialogue "Options d'impression"/Printing
    Preferences), PAS le DEVMODE par défaut implicite qu'utiliserait
    win32ui.CreatePrinterDC() — celui-ci correspond au format machine
    ("Options d'impression par défaut" dans Propriétés de l'imprimante),
    qui ignore le format d'étiquette personnalisé (ex: USER 49x150mm) réglé
    dans les préférences utilisateur. Sans ça, le pilote imprime sur un
    format générique (souvent Lettre/A4), d'où rognage et découpe absente.
    """
    if not HAS_WIN32PRINT:
        raise RuntimeError("pywin32 indisponible (pywin32 non installé ?)")

    handle = win32print.OpenPrinter(printer_name)
    try:
        # DocumentProperties a besoin d'un objet PyDEVMODE existant à
        # remplir (pas None) : on part de celui de GetPrinter, et on lui
        # demande de le mettre à jour EN PLACE (IN+OUT) avec les réglages
        # courants (dont le format d'étiquette personnalisé utilisateur).
        devmode = win32print.GetPrinter(handle, 2)["pDevMode"]
        win32print.DocumentProperties(
            0, handle, printer_name, devmode, devmode,
            win32con.DM_OUT_BUFFER | win32con.DM_IN_BUFFER,
        )

        # Nos étiquettes sont toujours composées en PAYSAGE côté serveur
        # (ex: 150mm × 48mm), quel que soit le format de MEDIA physique
        # déclaré dans le pilote (souvent portrait, ex: 49mm × 150mm — la
        # largeur du rouleau × la longueur de coupe). On force donc
        # l'orientation à LANDSCAPE : GDI fait alors pivoter la zone
        # imprimable de 90° pour nous (HORZRES/VERTRES échangés), exactement
        # comme le fait un navigateur qui imprime un PDF paysage sur une
        # imprimante configurée en portrait. Sans ça, l'image paysage est
        # étirée dans un cadre portrait (déformation, mauvaise longueur
        # coupée par le capteur de l'imprimante).
        devmode.Orientation = win32con.DMORIENT_LANDSCAPE
        fields_to_set = win32con.DM_ORIENTATION

        # Les préférences d'impression Windows (format de papier
        # personnalisé) sont enregistrées PAR COMPTE Windows : si ce service
        # tourne sous un compte différent de celui où le pilote a été
        # configuré manuellement (ex: LocalSystem), GetPrinter ci-dessus
        # renvoie un format par défaut différent, voire sans rapport (repéré
        # au tirage réel : étiquette imprimée à une taille incohérente, ex.
        # 75mm au lieu de 150mm). Quand le serveur nous fournit la taille
        # réelle de l'étiquette (admin Django, par imprimante), on la fixe
        # donc explicitement ici plutôt que de compter sur un état hérité du
        # compte appelant.
        if label_width_mm and label_height_mm:
            devmode.PaperWidth = round(label_width_mm * 10)
            devmode.PaperLength = round(label_height_mm * 10)
            fields_to_set |= win32con.DM_PAPERWIDTH | win32con.DM_PAPERLENGTH

        # On réapplique DocumentProperties après modification pour que le
        # pilote recalcule tout de façon cohérente (pas juste patcher les
        # champs bruts) et on marque les champs modifiés comme "à jour".
        devmode.Fields = devmode.Fields | fields_to_set
        win32print.DocumentProperties(
            0, handle, printer_name, devmode, devmode,
            win32con.DM_OUT_BUFFER | win32con.DM_IN_BUFFER,
        )
    finally:
        win32print.ClosePrinter(handle)

    # win32gui.CreateDC accepte un DEVMODE explicite (contrairement à
    # win32ui.CreateDC().CreatePrinterDC()) mais n'expose pas StartDoc/
    # EndDoc (fonctions spouleur GDI, pas de simples appels gdi32) : on
    # enveloppe donc le HDC brut avec win32ui pour récupérer ces méthodes.
    raw_hdc = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
    dc = win32ui.CreateDCFromHandle(raw_hdc)
    try:
        dc.StartDoc("Etiquette")
        dc.StartPage()

        width = dc.GetDeviceCaps(win32con.HORZRES)
        height = dc.GetDeviceCaps(win32con.VERTRES)

        dib = ImageWin.Dib(image)
        dib.draw(dc.GetHandleOutput(), (0, 0, width, height))

        dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()


def try_print(images: list, printers: list) -> tuple:
    """
    Essaie d'imprimer TOUTES les images (dans l'ordre) sur une même
    imprimante, en tentant les imprimantes candidates dans l'ordre fourni
    par le serveur (déjà trié par séquence). S'arrête à la première
    imprimante où toutes les images sont passées avec succès.

    Chaque imprimante DOIT être installée comme imprimante Windows classique
    (windows_printer_name renseigné) : il n'y a plus de mode d'envoi
    alternatif (plus de socket TCP brut), pour que toutes les étiquettes
    fonctionnent de la même façon quel que soit le site/imprimante.

    Retourne (succès, nom_imprimante, message_erreur).
    """
    if not printers:
        return False, "", "Aucune imprimante fournie par le serveur"
    if not images:
        return False, "", "Aucune étiquette à imprimer"

    errors = []
    for p in printers:
        name = p.get("name", "?")
        printer_name = p.get("windows_printer_name")
        if not printer_name:
            errors.append(f"{name}: pas de windows_printer_name configuré dans l'admin")
            continue
        try:
            logger.info("Impression sur '%s' (%s) — %d étiquette(s)", name, printer_name, len(images))
            for i, image in enumerate(images):
                if i > 0:
                    time.sleep(PRINT_JOB_DELAY)
                _print_image_gdi(
                    image, printer_name,
                    label_width_mm=p.get("label_width_mm"),
                    label_height_mm=p.get("label_height_mm"),
                )
            logger.info("Imprimé sur %s", name)
            return True, name, ""
        except Exception as exc:
            logger.warning("Échec sur %s (%s) : %s", name, printer_name, exc)
            errors.append(f"{name}: {exc}")

    return False, "", "Aucune imprimante joignable — " + " | ".join(errors)


def poll_once(session: requests.Session, server: str) -> Optional[dict]:
    """
    Effectue un poll (long polling). Retourne le job (dict) ou None si 204.
    Lève requests.RequestException en cas d'erreur réseau.
    """
    resp = session.post(f"{server}/api/labels/agent/poll/", timeout=POLL_HTTP_TIMEOUT)

    if resp.status_code == 204:
        return None
    if resp.status_code == 401:
        raise RuntimeError("Token invalide (401) — vérifier la config de l'agent")
    resp.raise_for_status()
    return resp.json()


def send_ack(session: requests.Session, server: str,
             job_id: int, success: bool, printed_on: str = "", error: str = "") -> None:
    """Confirme le résultat de l'impression au serveur."""
    body = {"job_id": job_id, "success": success}
    if success:
        body["printed_on"] = printed_on
    else:
        body["error"] = error
    resp = session.post(f"{server}/api/labels/agent/ack/", json=body, timeout=15)
    resp.raise_for_status()


def run(server: str, token: str) -> None:
    """Boucle principale de l'agent."""
    if not HAS_WIN32PRINT:
        raise RuntimeError(
            "pywin32 et/ou Pillow indisponibles — installer les dépendances "
            "(pip install -r requirements.txt) avant de lancer l'agent."
        )

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    logger.info("Agent démarré. Serveur : %s", server)

    while True:
        try:
            job = poll_once(session, server)

            if job is None:
                # 204 : pas de job, on repoll immédiatement (le long polling
                # a déjà "consommé" jusqu'à 30s d'attente côté serveur).
                continue

            job_id = job["job_id"]
            images = [
                Image.open(BytesIO(base64.b64decode(b64)))
                for b64 in job["label_images"]
            ]
            printers = job.get("printers", [])
            logger.info(
                "Job #%s reçu (%d étiquette(s), %d imprimante(s) candidate(s))",
                job_id, len(images), len(printers),
            )

            success, printed_on, error = try_print(images, printers)

            send_ack(session, server, job_id, success, printed_on, error)
            logger.info("ACK envoyé pour job #%s (success=%s)", job_id, success)
            # On repoll immédiatement pour enchaîner d'éventuels jobs en attente.

        except requests.RequestException as exc:
            logger.error("Erreur réseau (serveur injoignable ?) : %s", exc)
            logger.info("Nouvelle tentative dans %ss", NETWORK_RETRY_DELAY)
            time.sleep(NETWORK_RETRY_DELAY)

        except RuntimeError as exc:
            # Erreur fatale de config (token invalide) : inutile de boucler vite.
            logger.error("%s", exc)
            time.sleep(NETWORK_RETRY_DELAY)

        except Exception as exc:
            logger.exception("Erreur inattendue : %s", exc)
            time.sleep(NETWORK_RETRY_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Agent d'impression (pull)")
    parser.add_argument("--config", default="agent.ini", help="Fichier de config")
    parser.add_argument("--server", help="URL du serveur (surcharge le .ini)")
    parser.add_argument("--token", help="Token de l'agent (surcharge le .ini)")
    parser.add_argument(
        "--list-printers",
        action="store_true",
        help="Liste les imprimantes Windows installées puis quitte",
    )
    parser.add_argument(
        "--test-print-image",
        metavar="PNG_PATH",
        help="Imprime un fichier PNG directement (test GDI sans passer par le serveur), "
             "à utiliser avec --test-printer",
    )
    parser.add_argument(
        "--test-printer",
        metavar="NOM_WINDOWS",
        help="Nom Windows exact de l'imprimante pour --test-print-image (cf. --list-printers)",
    )
    parser.add_argument(
        "--devmode-info",
        metavar="NOM_WINDOWS",
        help="Diagnostic : affiche le format de papier/étiquette (DEVMODE) réellement "
             "utilisé pour cette imprimante, puis quitte.",
    )
    args = parser.parse_args()

    if args.list_printers:
        list_windows_printers()
        sys.exit(0)

    if args.devmode_info:
        print_devmode_info(args.devmode_info)
        sys.exit(0)

    if args.test_print_image:
        if not args.test_printer:
            logger.error("--test-printer est requis avec --test-print-image")
            sys.exit(1)
        image = Image.open(args.test_print_image)
        _print_image_gdi(image, args.test_printer)
        print(f"Image envoyée à l'impression sur '{args.test_printer}'")
        sys.exit(0)

    cfg = load_config(args.config)
    server = (args.server or cfg["server"]).rstrip("/")
    token = args.token or cfg["token"]

    if not server or not token:
        logger.error(
            "Config incomplète : 'server' et 'token' requis "
            "(via %s ou --server/--token)", args.config,
        )
        sys.exit(1)

    try:
        run(server, token)
    except KeyboardInterrupt:
        logger.info("Agent arrêté.")
        sys.exit(0)
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
