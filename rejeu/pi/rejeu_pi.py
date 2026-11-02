#!/usr/bin/env python3
"""Rejeu du tampon téléinfo vers Home Assistant — côté Pi.

Le Pi mesure en continu et écrit une ligne toutes les cinq minutes dans
`/var/lib/teleinfo/historique.db`. Quand la liaison MQTT tombe, ces mesures
restent locales : ce script les convertit en heures complètes et les publie sur
`teleinfo/rejeu`, où un correspondant côté Home Assistant les importe dans une
statistique externe.

Règles tenues par ce script :

- **Aucune heure inventée.** Une heure n'est envoyée que si elle contient au
  moins 10 des 12 créneaux de cinq minutes. Sinon elle est signalée et sautée.
- **Aucune heure incomplète comptée comme complète dans le cumul.** L'origine
  du cumul (`sum = index − origine`) est fixée une fois pour toutes dans la
  table `ack`, ce qui rend le rejeu idempotent : renvoyer deux fois la même
  heure produit exactement les mêmes valeurs.
- **Acquittement obligatoire.** Le filigrane n'avance qu'après un accusé de
  réception de Home Assistant. Une heure non acquittée sera renvoyée.

Usage : rejeu_pi.py [--dry-run] [--tout]
"""
import json
import sqlite3
import sys
import time

import paho.mqtt.client as mqtt

BASE = "/var/lib/teleinfo/historique.db"
CONF_MQTT = "/etc/teleinfo/mqtt.conf"
HOTES = ["192.168.0.78"]           # le broker, sur Corneille
SUJET = "teleinfo/rejeu"
SUJET_ACK = "teleinfo/rejeu/ack"
CRENEAU = 300                       # secondes
MIN_ECHANTILLONS = 10               # sur 12 pour déclarer l'heure complète
LOT = 48                            # heures par message


def identifiants():
    user = password = None
    for ligne in open(CONF_MQTT):
        if ligne.startswith("user"):
            user = ligne.split(":", 1)[1].strip().strip('"')
        elif ligne.startswith("password"):
            password = ligne.split(":", 1)[1].strip().strip('"')
    return user, password


def preparer(connexion):
    connexion.execute("CREATE TABLE IF NOT EXISTS ack ("
                      "cle TEXT PRIMARY KEY, valeur TEXT)")
    connexion.commit()


def lire_ack(connexion, cle):
    ligne = connexion.execute("SELECT valeur FROM ack WHERE cle=?", (cle,)).fetchone()
    return ligne[0] if ligne else None


def ecrire_ack(connexion, cle, valeur):
    connexion.execute("INSERT OR REPLACE INTO ack (cle, valeur) VALUES (?,?)",
                      (cle, str(valeur)))
    connexion.commit()


def heures_completes(connexion, depuis):
    """Regroupe les mesures par heure UTC et ne garde que les heures complètes."""
    lignes = list(connexion.execute(
        "SELECT ts, base FROM mesures WHERE ts >= ? AND base IS NOT NULL ORDER BY ts",
        (depuis,)))
    par_heure = {}
    for ts, base in lignes:
        debut = ts - ts % 3600
        par_heure.setdefault(debut, []).append((ts, base))

    resultat, incompletes = [], []
    for debut in sorted(par_heure):
        echantillons = par_heure[debut]
        if len(echantillons) < MIN_ECHANTILLONS:
            incompletes.append((debut, len(echantillons)))
            continue
        resultat.append((debut, echantillons[-1][1]))
    return resultat, incompletes


def main():
    simule = "--dry-run" in sys.argv
    tout = "--tout" in sys.argv

    connexion = sqlite3.connect(BASE, timeout=15)
    preparer(connexion)

    origine = lire_ack(connexion, "origine")
    if origine is not None:
        origine = float(origine)
    filigrane = lire_ack(connexion, "filigrane")
    depuis = 0 if tout else (int(filigrane) if filigrane else 0)

    heures, incompletes = heures_completes(connexion, depuis)
    if incompletes:
        print(f"{len(incompletes)} heure(s) incomplète(s), non envoyées : "
              + ", ".join(time.strftime("%d/%m %Hh", time.localtime(d)) + f" ({n}/12)"
                          for d, n in incompletes[:6]), flush=True)

    if not heures:
        connexion.close()
        return 0

    if origine is None:
        origine = heures[0][1]
        if not simule:
            ecrire_ack(connexion, "origine", f"{origine:.3f}")

    lots = [heures[i:i + LOT] for i in range(0, len(heures), LOT)]
    if simule:
        for lot in lots:
            print(f"[simulation] {len(lot)} heures : "
                  f"{time.strftime('%d/%m %Hh', time.gmtime(lot[0][0]))} → "
                  f"{time.strftime('%d/%m %Hh', time.gmtime(lot[-1][0]))}, "
                  f"index {lot[0][1]:.3f} → {lot[-1][1]:.3f} kWh", flush=True)
        connexion.close()
        return 0

    user, password = identifiants()
    accuse = {}

    def sur_ack(_c, _u, message):
        try:
            donnees = json.loads(message.payload.decode())
        except Exception:
            return
        accuse[donnees.get("demande")] = donnees

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rejeu-pi")
    client.username_pw_set(user, password)
    client.on_message = sur_ack
    hote = None
    for candidat in HOTES:
        try:
            client.connect(candidat, 1883, 60)
            hote = candidat
            break
        except Exception:
            continue
    if hote is None:
        print("broker injoignable : rejeu reporté", flush=True)
        connexion.close()
        return 1
    client.subscribe(SUJET_ACK)
    client.loop_start()

    for numero, lot in enumerate(lots):
        demande = f"{lot[0][0]}-{lot[-1][0]}"
        charge = {"demande": demande,
                  "origine": round(origine, 3),
                  "heures": [{"start": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                     time.gmtime(debut)),
                              "state": round(index, 3),
                              "sum": round(index - origine, 3)}
                             for debut, index in lot]}
        client.publish(SUJET, json.dumps(charge))
        limite = time.time() + 120
        while time.time() < limite and demande not in accuse:
            time.sleep(1)
        reponse = accuse.get(demande)
        if not reponse or not reponse.get("ok"):
            motif = (reponse or {}).get("motif", "pas d'accusé")
            print(f"lot {numero + 1}/{len(lots)} non acquitté ({motif}) : arrêt, "
                  f"il sera renvoyé au prochain passage", flush=True)
            client.loop_stop()
            connexion.close()
            return 1
        ecrire_ack(connexion, "filigrane", lot[-1][0] + 3600)
        print(f"lot {numero + 1}/{len(lots)} acquitté jusqu'à "
              f"{time.strftime('%d/%m %Hh', time.gmtime(lot[-1][0]))}", flush=True)

    client.loop_stop()
    client.disconnect()
    connexion.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
