#!/usr/bin/env python3
"""Rejeu des mesures téléinfo vers les statistiques Home Assistant — côté HA.

Pourquoi ce script : le Pi accumule ses mesures dans un tampon SQLite, mais rien
ne les renvoyait jamais à Home Assistant. Un redémarrage du Pi laissait donc un
trou dans la répartition horaire, définitivement.

Principe : le Pi publie les heures manquantes sur `teleinfo/rejeu` (MQTT). Ce
script les reçoit, les valide, les importe dans une statistique EXTERNE
(`teleinfo:energie_base`), vérifie que l'écriture a bien eu lieu, puis acquitte.
Le Pi ne considère une heure comme transmise qu'après cet acquittement.

Pourquoi une statistique externe et pas `sensor.linky_index_stable` : celle-ci
est compilée par le recorder de Home Assistant. Y écrire à la main provoquerait
une concurrence entre deux auteurs, exactement le défaut qui nous a coûté deux
jours de série instable.
"""
import calendar
import json
import os
import subprocess
import sys
import time

import paho.mqtt.client as mqtt

CONF_MQTT = "/home/corneille/.mqtt-sante.conf"
HOTE_MQTT = "192.168.0.78"
STATISTIQUE = "teleinfo:energie_base"
SOURCE = "teleinfo"
SUJET_REJEU = "teleinfo/rejeu"
SUJET_ACK = "teleinfo/rejeu/ack"


def identifiants():
    user = password = None
    for ligne in open(CONF_MQTT):
        if ligne.startswith("user:"):
            user = ligne.split(":", 1)[1].strip()
        elif ligne.startswith("password:"):
            password = ligne.split(":", 1)[1].strip()
    return user, password


def jeton():
    sortie = subprocess.run(["docker", "inspect", "ha-linky", "--format",
                             "{{range .Config.Env}}{{println .}}{{end}}"],
                            capture_output=True, text=True, timeout=60).stdout
    for ligne in sortie.splitlines():
        if ligne.startswith("HASS_TOKEN="):
            return ligne.split("=", 1)[1]
    return ""


def ws(frames, timeout=180):
    entree = "\n".join(json.dumps(f) for f in frames) + "\n"
    sortie = subprocess.run(["docker", "exec", "-i", "-e", "HASS_TOKEN", "homeassistant",
                             "python3", "/tmp/ws_ha.py"],
                            input=entree, capture_output=True, text=True, timeout=timeout,
                            env={"PATH": "/usr/bin:/bin", "HASS_TOKEN": jeton()})
    return [json.loads(l) for l in sortie.stdout.splitlines() if l.strip().startswith("{")]


def valider(heures):
    """Refuse tout lot incohérent plutôt que d'écrire n'importe quoi."""
    if not heures:
        return "lot vide"
    precedente = None
    for h in heures:
        if not all(k in h for k in ("start", "state", "sum")):
            return f"heure incomplète : {h}"
        debut = calendar.timegm(time.strptime(h["start"][:19], "%Y-%m-%dT%H:%M:%S"))
        if debut % 3600:
            return f"heure non alignée : {h['start']}"
        if precedente is not None and debut != precedente + 3600:
            return f"trou dans le lot : {h['start']}"
        if precedente is not None and h["state"] < heures[heures.index(h) - 1]["state"]:
            return f"index qui recule : {h['start']}"
        precedente = debut
    # cohérence sum/state : sum = state − origine, l'origine étant commune au lot
    origine = round(heures[0]["state"] - heures[0]["sum"], 3)
    for h in heures:
        if abs(round(h["state"] - h["sum"], 3) - origine) > 0.002:
            return f"incohérence state/sum à {h['start']}"
    return None


def importer(heures):
    meta = {"statistic_id": STATISTIQUE, "source": SOURCE, "name": "Énergie base (téléinfo)",
            "unit_of_measurement": "kWh", "has_mean": False, "has_sum": True,
            "mean_type": 0, "unit_class": "energy"}
    stats = [{"start": h["start"], "state": h["state"], "sum": h["sum"]} for h in heures]
    reponses = ws([{"type": "recorder/import_statistics", "metadata": meta, "stats": stats}])
    return any(r.get("success") for r in reponses)


def relire(heures):
    """import_statistics répond « success » avant l'écriture réelle : on vérifie."""
    debut = heures[0]["start"]
    reponses = ws([{"type": "recorder/statistics_during_period", "start_time": debut,
                    "statistic_ids": [STATISTIQUE], "period": "hour", "types": ["state"]}])
    resultat = next((r["result"] for r in reponses if r.get("success")), {})
    ecrites = {p["start"] for p in resultat.get(STATISTIQUE, [])}
    attendues = set()
    for h in heures:
        attendues.add(calendar.timegm(time.strptime(h["start"][:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
    return attendues.issubset(ecrites), len(attendues & ecrites), len(attendues)


def main():
    simule = "--dry-run" in sys.argv
    user, password = identifiants()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rejeu-ha")
    client.username_pw_set(user, password)

    def sur_message(cli, _u, message):
        try:
            charge = json.loads(message.payload.decode())
        except Exception as e:
            print("lot illisible :", e, flush=True)
            return
        heures = charge.get("heures", [])
        demande = charge.get("demande", "?")
        erreur = valider(heures)
        if erreur:
            print(f"lot refusé ({demande}) : {erreur}", flush=True)
            cli.publish(SUJET_ACK, json.dumps({"demande": demande, "ok": False,
                                               "motif": erreur}))
            return
        if simule:
            print(f"[simulation] {len(heures)} heures valides, de {heures[0]['start']} "
                  f"à {heures[-1]['start']}", flush=True)
            return
        if not importer(heures):
            print(f"import refusé par Home Assistant ({demande})", flush=True)
            cli.publish(SUJET_ACK, json.dumps({"demande": demande, "ok": False,
                                               "motif": "import refusé"}))
            return
        time.sleep(2)
        ok, ecrites, attendues = relire(heures)
        if not ok:
            print(f"écriture incomplète ({demande}) : {ecrites}/{attendues} heures "
                  f"retrouvées en base", flush=True)
            cli.publish(SUJET_ACK, json.dumps({"demande": demande, "ok": False,
                                               "motif": f"{ecrites}/{attendues}"}))
            return
        print(f"rejoué : {attendues} heures jusqu'à {heures[-1]['start']}", flush=True)
        cli.publish(SUJET_ACK, json.dumps({"demande": demande, "ok": True,
                                           "jusqu_a": heures[-1]["start"]}))

    def sur_connexion(cli, _u, _f, raison, _p):
        # Se réabonner à chaque connexion : après une coupure du broker ou une
        # reconnexion, l'abonnement précédent est perdu et le service resterait
        # muet tout en paraissant actif.
        cli.subscribe(SUJET_REJEU)
        print(f"connecté au broker ({raison}), abonné à {SUJET_REJEU}", flush=True)

    client.on_connect = sur_connexion
    client.on_message = sur_message
    client.connect(HOTE_MQTT, 1883, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
