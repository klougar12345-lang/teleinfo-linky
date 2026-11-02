#!/usr/bin/env python3
"""PiTInfo → MQTT : lit la TIC du Linky et publie dans Home Assistant.

Détecte automatiquement le mode :
  historique  1200 bds 7E1  (lignes LABEL VALEUR CHECKSUM\\r)
  standard    9600 bds 7E1  (trame 0x02 ... 0x03 0x0A, champs tabulés)
"""
import json
import os
import re
import sqlite3
import sys
import time

import paho.mqtt.client as mqtt
import serial

PORT = "/dev/serial0"
CONF = "/etc/teleinfo/mqtt.conf"
HOST = "192.168.0.78"
DEVICE = {
    "identifiers": ["pitinfo_linky"],
    "name": "Linky (PiTInfo)",
    "manufacturer": "ch2i",
    "model": "Téléinfo Pi v1.4",
}

CAPTEURS = {
    "PAPP":   ("Puissance apparente", "VA", "apparent_power", "measurement", "mdi:flash"),
    "IINST":  ("Intensité", "A", "current", "measurement", "mdi:current-ac"),
    "ISOUSC": ("Abonnement", "A", "current", None, "mdi:fuse"),
    "BASE":   ("Index base", "kWh", "energy", "total_increasing", "mdi:counter"),
    "HCHC":   ("Index heures creuses", "kWh", "energy", "total_increasing", "mdi:counter"),
    "HCHP":   ("Index heures pleines", "kWh", "energy", "total_increasing", "mdi:counter"),
    "PTEC":   ("Tarif en cours", None, None, None, "mdi:clock-outline"),
}

MOTIF_LABEL = re.compile(r"^[A-Z0-9_]{2,9}$")


def lire_conf():
    utilisateur = motdepasse = None
    with open(CONF) as f:
        for ligne in f:
            if ligne.startswith("user:"):
                utilisateur = ligne.split(":", 1)[1].strip()
            elif ligne.startswith("password:"):
                motdepasse = ligne.split(":", 1)[1].strip()
    return utilisateur, motdepasse


def ouvrir(bauds):
    return serial.Serial(
        PORT, baudrate=bauds, bytesize=7, parity=serial.PARITY_EVEN,
        stopbits=1, timeout=2, exclusive=True,
    )


def publier_decouverte(client):
    for label, (nom, unite, dclass, sclass, icone) in CAPTEURS.items():
        cfg = {
            "name": nom,
            "unique_id": f"linky_{label.lower()}",
            "state_topic": f"teleinfo/{label}",
            "icon": icone,
            "device": DEVICE,
        }
        if unite:
            cfg["unit_of_measurement"] = unite
        if dclass:
            cfg["device_class"] = dclass
        if sclass:
            cfg["state_class"] = sclass
        if dclass == "energy":
            # Garde-fou : une valeur aberrante (index publie en Wh par erreur,
            # ou message retenu perime) casserait la statistique de compteur.
            cfg["value_template"] = (
                "{{ [value | float(0), this.state | float(0)] | max if (value | float(0)) < 200000 else this.state }}"
            )
        client.publish(f"homeassistant/sensor/linky_{label.lower()}/config",
                       json.dumps(cfg), retain=True)

    client.publish("homeassistant/sensor/linky_adco/config", json.dumps({
        "name": "Compteur (ADCO)", "unique_id": "linky_adco",
        "state_topic": "teleinfo/ADCO", "icon": "mdi:identifier",
        "device": DEVICE}), retain=True)



# --- tampon local : 8 jours de mesures, au cas où MQTT ou Home Assistant tombe ---
BASE_DONNEES = "/var/lib/teleinfo/historique.db"
PERIODE_ECRITURE = 300      # une ligne toutes les 5 minutes
RETENTION = 400 * 86400   # un an : le volume est négligeable (~15 Mo)
_derniere_ecriture = 0
_valeurs = {}


def init_tampon():
    try:
        os.makedirs(os.path.dirname(BASE_DONNEES), exist_ok=True)
        with sqlite3.connect(BASE_DONNEES, timeout=10) as cx:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.execute("PRAGMA synchronous=NORMAL")
            cx.execute("CREATE TABLE IF NOT EXISTS mesures ("
                       "ts INTEGER PRIMARY KEY, base REAL, papp INTEGER, iinst INTEGER)")
            cx.commit()
        print("tampon local prêt", flush=True)
    except Exception as erreur:
        print(f"tampon indisponible : {erreur}", flush=True)


def ecrire_tampon():
    global _derniere_ecriture
    maintenant = int(time.time())
    if maintenant - _derniere_ecriture < PERIODE_ECRITURE:
        return
    _derniere_ecriture = maintenant
    try:
        with sqlite3.connect(BASE_DONNEES, timeout=10) as cx:
            cx.execute("INSERT OR REPLACE INTO mesures (ts, base, papp, iinst) VALUES (?,?,?,?)",
                       (maintenant - maintenant % PERIODE_ECRITURE, _valeurs.get("BASE"), _valeurs.get("PAPP"), _valeurs.get("IINST")))
            # Ne jamais purger au-delà du filigrane de rejeu : une mesure non
            # encore transmise à Home Assistant ne doit pas être détruite parce
            # qu'elle a vieilli. On garde donc tout ce qui suit la dernière
            # heure acquittée, même au-delà de la rétention.
            try:
                ligne = cx.execute("SELECT valeur FROM ack WHERE cle='filigrane'").fetchone()
                filigrane = int(ligne[0]) if ligne else 0
            except Exception:
                filigrane = 0
            limite = maintenant - RETENTION
            if filigrane:
                limite = min(limite, filigrane)
            cx.execute("DELETE FROM mesures WHERE ts < ?", (limite,))
            cx.commit()
    except Exception as erreur:
        print(f"ecriture tampon impossible : {erreur}", flush=True)


def publier(client, label, valeur):
    """Publie une valeur normalisée : index en kWh, courants et puissance en entiers."""
    valeur = valeur.strip()
    if label in ("BASE", "HCHC", "HCHP"):
        try:
            valeur = f"{int(valeur) / 1000:.3f}"
        except ValueError:
            return
    elif label in ("PAPP", "IINST", "ISOUSC", "IMAX"):
        try:
            valeur = str(int(valeur))
        except ValueError:
            return
    client.publish(f"teleinfo/{label}", valeur, retain=False)
    if label in ("BASE", "PAPP", "IINST"):
        try:
            _valeurs[label] = float(valeur) if label == "BASE" else int(valeur)
        except ValueError:
            pass
        if label == "BASE":
            ecrire_tampon()


def boucle_historique(ser, client):
    tampon = b""
    while True:
        tampon += ser.read(256)
        while b"\r" in tampon:
            ligne, tampon = tampon.split(b"\r", 1)
            texte = ligne.decode("ascii", "ignore")
            texte = texte.replace("\x02", " ").replace("\x03", " ").replace("\n", " ").strip()
            if not texte:
                continue
            morceaux = texte.split(" ")
            if len(morceaux) >= 2 and MOTIF_LABEL.match(morceaux[0]):
                publier(client, morceaux[0], morceaux[1].strip())


def boucle_standard(ser, client):
    tampon = b""
    while True:
        tampon += ser.read(256)
        while b"\x0a" in tampon:
            trame, tampon = tampon.split(b"\x0a", 1)
            trame = trame.replace(b"\x02", b"").replace(b"\x03", b"")
            champs = trame.split(b"\x09")
            for i in range(0, len(champs) - 1, 3):
                label = champs[i].decode("ascii", "ignore").strip()
                valeur = champs[i + 1].decode("ascii", "ignore").strip()
                if label and valeur and MOTIF_LABEL.match(label):
                    publier(client, label, valeur)


def main():
    # Le tampon AVANT tout le reste : ni le réseau ni la configuration ne
    # doivent pouvoir empêcher la mesure et l'enregistrement. Une passerelle
    # qui démarre sans WiFi, ou avec un fichier de conf cassé, doit continuer
    # à tamponner — seule la publication MQTT est perdue.
    init_tampon()

    try:
        utilisateur, motdepasse = lire_conf()
    except Exception as erreur:
        print(f"configuration MQTT illisible : {erreur} — tampon actif, publication désactivée", flush=True)
        utilisateur, motdepasse = None, None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pitinfo")
    client.username_pw_set(utilisateur, motdepasse)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(cli, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            print(f"MQTT connecté via {HOST}", flush=True)
            publier_decouverte(cli)
        else:
            print(f"MQTT refusé (code {reason_code})", flush=True)

    client.on_connect = on_connect

    # connect_async : ne bloque pas et ne quitte pas si le broker est absent ;
    # paho retente indéfiniment, et on_connect republie la découverte.
    client.connect_async(HOST, 1883, 60)
    client.loop_start()
    print(f"MQTT en cours de connexion vers {HOST}", flush=True)

    while True:
        for bauds in (1200, 9600):
            try:
                ser = ouvrir(bauds)
            except Exception as erreur:
                print(f"port indisponible : {erreur}", flush=True)
                time.sleep(5)
                break
            print(f"écoute à {bauds} bds…", flush=True)
            debut = time.time()
            recu = b""
            while time.time() - debut < 6:
                recu += ser.read(128)
            ser.close()
            if not recu.strip(b"\x00").strip():
                print(f"aucun signal à {bauds} bds", flush=True)
                continue
            print(f"signal détecté à {bauds} bds", flush=True)
            ser = ouvrir(bauds)
            if bauds == 1200:
                boucle_historique(ser, client)
            else:
                boucle_standard(ser, client)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
