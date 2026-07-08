#!/usr/bin/env python3
"""
Agent d'impression (modèle PULL / long polling).

Tourne sur un poste client. Boucle :
  1. POLL le serveur (long polling ~30s) → récupère un job (ZPL + imprimantes)
  2. IMPRIME sur la première imprimante joignable (socket TCP 9100)
  3. ACK le serveur (succès + imprimante utilisée, ou échec + raison)
  4. Recommence

Connexion SORTANTE uniquement (compatible NAT/pare-feu d'entreprise) :
le serveur n'initie jamais rien vers l'agent.

Config : fichier agent.ini par défaut, surchargeable en CLI.
    python agent.py                          # utilise agent.ini
    python agent.py --server https://... --token XXX

Dépendances : requests (pip install requests)
"""
from __future__ import annotations
import argparse
import configparser
import logging
import socket
import sys
import time
from pathlib import Path
from typing import Optional


import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENT] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Délai d'attente réseau avant de réessayer un poll quand le serveur est
# injoignable (erreur réseau, VPS down). Évite de marteler en boucle serrée.
NETWORK_RETRY_DELAY = 5

# Timeout de connexion TCP à une imprimante avant de passer à la suivante.
PRINTER_CONNECT_TIMEOUT = 5

# Timeout de la requête de poll : doit être > au long-poll serveur (30s) pour
# laisser le serveur tenir la connexion ouverte. On met une marge.
POLL_HTTP_TIMEOUT = 45


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


def print_zpl_to_printer(zpl: str, host: str, port: int) -> None:
    """
    Envoie le ZPL brut à une imprimante réseau via socket TCP (protocole RAW
    port 9100). Lève une exception si la connexion échoue ou timeout.

    Le shutdown(SHUT_WR) signale la fin d'envoi (l'imprimante n'attend pas
    d'ACK applicatif en RAW ; on ferme proprement le canal d'écriture).
    """
    with socket.create_connection((host, port), timeout=PRINTER_CONNECT_TIMEOUT) as sock:
        sock.sendall(zpl.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)


def try_print(zpl: str, printers: list) -> tuple:
    """
    Essaie d'imprimer sur les imprimantes dans l'ordre reçu (déjà trié par
    séquence côté serveur). S'arrête à la première JOIGNABLE.

    Retourne (succès, nom_imprimante_utilisée, message_erreur).
    """
    if not printers:
        return False, "", "Aucune imprimante fournie par le serveur"

    errors = []
    for p in printers:
        name = p.get("name", "?")
        host = p.get("host")
        port = int(p.get("port", 9100))
        try:
            logger.info("Tentative impression sur %s (%s:%s)", name, host, port)
            print_zpl_to_printer(zpl, host, port)
            logger.info("Imprimé sur %s", name)
            return True, name, ""
        except Exception as exc:
            logger.warning("Échec sur %s (%s:%s) : %s", name, host, port, exc)
            errors.append(f"{name}: {exc}")

    return False, "", "Aucune imprimante joignable — " + " | ".join(errors)


def poll_once(session: requests.Session, server: str) -> Optional[dict]:

    """
    Effectue un poll (long polling). Retourne le job (dict) ou None si 204.
    Lève requests.RequestException en cas d'erreur réseau.
    """
    resp = session.post(f"{server}/labels/agent/poll/", timeout=POLL_HTTP_TIMEOUT)

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
    resp = session.post(f"{server}/labels/agent/ack/", json=body, timeout=15)
    resp.raise_for_status()


def run(server: str, token: str) -> None:
    """Boucle principale de l'agent."""
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
            zpl = job["zpl"]
            printers = job.get("printers", [])
            logger.info("Job #%s reçu (%d imprimante(s) candidate(s))", job_id, len(printers))

            success, printed_on, error = try_print(zpl, printers)

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
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()