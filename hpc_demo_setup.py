#!/usr/bin/env python3
"""
hpc_demo_setup.py — NetBox 4.2.9-ready HPC demo seed

What this does:
- Creates custom fields (cluster_name [text], hpc_role [select via choice set], estimated_watts [int])
- Creates tags, roles, site, rack, device types
- Builds a sample rack with GPU/CPU/Storage nodes, a PDU, ToR and Aggregation switches
- Adds interfaces (with proper type slugs), power ports/outlets, data and power cabling
- Prints rack power roll-up, ToR utilization, and a robust path trace (GPU → ToR → Agg)

Env:
  NETBOX_URL   (e.g., https://netbox.example.com)
  NETBOX_TOKEN (API token with write perms)

Deps:
  pip install pynetbox requests python-slugify
"""

import os
import sys
import json
import requests
import pynetbox
from slugify import slugify

NETBOX_URL = os.environ.get("NETBOX_URL", "").rstrip("/")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "")

if not NETBOX_URL or not NETBOX_TOKEN:
    print("ERROR: Please export NETBOX_URL and NETBOX_TOKEN", file=sys.stderr)
    sys.exit(1)

nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)

# ----------------------------- utilities -----------------------------
def get_or_create(endpoint, **attrs):
    """Generic idempotent creator by (name|slug)."""
    key = None
    for k in ("name", "slug"):
        if k in attrs and attrs[k]:
            key = {k: attrs[k]}
            break
    obj = endpoint.get(**(key or {}))
    if obj:
        return obj
    return endpoint.create(attrs)

def ensure_tag(name: str, slug_: str):
    t = nb.extras.tags.get(slug=slug_)
    if t:
        return t
    return nb.extras.tags.create({"name": name, "slug": slug_})

def ensure_device_role(name: str, slug_: str):
    r = nb.dcim.device_roles.get(slug=slug_)
    if r:
        return r
    return nb.dcim.device_roles.create({"name": name, "slug": slug_})

def ensure_manufacturer(name: str):
    return get_or_create(nb.dcim.manufacturers, name=name, slug=slugify(name))

def ensure_device_type(manufacturer, model: str, u: int = 1, full_depth: bool = True):
    slug_ = slugify(model)
    dt = nb.dcim.device_types.get(slug=slug_)
    if dt:
        return dt
    return nb.dcim.device_types.create({
        "manufacturer": manufacturer.id,
        "model": model,
        "slug": slug_,
        "u_height": u,
        "is_full_depth": full_depth,
    })

# ------------------ CustomFieldChoiceSet (NetBox 4.2.9) ------------------
def _vals_from_choices(choices):
    """Extract values from 'choices' whether list-of-lists or list-of-dicts."""
    vals = set()
    for c in choices or []:
        if isinstance(c, (list, tuple)) and c:
            vals.add(c[0])  # ["gpu","gpu"] -> "gpu"
        elif isinstance(c, dict):
            vals.add(c.get("value"))
    return vals

def _pynetbox_has_choice_sets():
    return hasattr(nb.extras, "custom_field_choice_sets") and nb.extras.custom_field_choice_sets is not None

def ensure_choice_set(name: str, values):
    """
    Create or augment a CustomFieldChoiceSet named `name` with `values`.
    NetBox 4.2.9 expects: extra_choices = [["gpu","gpu"], ...] (list-of-lists).
    """
    payload_choices_ll = [[v, v] for v in values]

    if _pynetbox_has_choice_sets():
        csets = nb.extras.custom_field_choice_sets
        cs = csets.get(name=name)
        if cs:
            existing_vals = _vals_from_choices(getattr(cs, "choices", []))
            missing = [v for v in values if v not in existing_vals]
            if not missing:
                return cs

            current_extra = getattr(cs, "extra_choices", []) or []
            norm_extra = []
            for c in current_extra:
                if isinstance(c, (list, tuple)):
                    norm_extra.append(list(c))
                elif isinstance(c, dict):
                    norm_extra.append([c.get("value"), c.get("label", c.get("value"))])

            updated = csets.update([{
                "id": cs.id,
                # Only patch extra_choices; omit base_choices entirely
                "extra_choices": norm_extra + [[v, v] for v in missing],
            }])[0]
            return updated

        # Create new set (omit base_choices)
        return csets.create({"name": name, "extra_choices": payload_choices_ll})

    # Fallback: direct requests (older pynetbox)
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {NETBOX_TOKEN}", "Content-Type": "application/json"})
    url = f"{NETBOX_URL}/api/extras/custom-field-choice-sets/"

    r = session.get(url, params={"name": name, "limit": 0}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("count", 0) > 0:
        cs = data["results"][0]
        existing_vals = _vals_from_choices(cs.get("choices", []))
        missing = [v for v in values if v not in existing_vals]
        if not missing:
            return cs
        current_extra = cs.get("extra_choices") or []
        norm_extra = []
        for c in current_extra:
            if isinstance(c, list):
                norm_extra.append(c)
            elif isinstance(c, dict):
                norm_extra.append([c.get("value"), c.get("label", c.get("value"))])
        patch = {"extra_choices": norm_extra + [[v, v] for v in missing]}
        r2 = session.patch(f"{url}{cs['id']}/", data=json.dumps(patch), timeout=30)
        r2.raise_for_status()
        return r2.json()

    r = session.post(url, data=json.dumps({"name": name, "extra_choices": payload_choices_ll}), timeout=30)
    r.raise_for_status()
    return r.json()

# ---------------------- Custom Fields (devices) ----------------------
CF_DEVICE_OBJECT_TYPES = ["dcim.device"]

def ensure_cf_text(name: str, label: str):
    cf = nb.extras.custom_fields.get(name=name)
    payload = {"name": name, "type": "text", "label": label, "required": False, "object_types": CF_DEVICE_OBJECT_TYPES}
    return cf if cf else nb.extras.custom_fields.create(payload)

def ensure_cf_int(name: str, label: str):
    cf = nb.extras.custom_fields.get(name=name)
    payload = {"name": name, "type": "integer", "label": label, "required": False, "object_types": CF_DEVICE_OBJECT_TYPES}
    return cf if cf else nb.extras.custom_fields.create(payload)

def ensure_cf_select(name: str, label: str, values, choice_set_name=None):
    if not choice_set_name:
        choice_set_name = name
    cs = ensure_choice_set(choice_set_name, values)
    cs_id = cs["id"] if isinstance(cs, dict) else cs.id

    cf = nb.extras.custom_fields.get(name=name)
    payload = {
        "name": name, "type": "select", "label": label, "required": False,
        "object_types": CF_DEVICE_OBJECT_TYPES, "choice_set": cs_id, "selection_mode": "single",
    }
    if not cf:
        return nb.extras.custom_fields.create(payload)

    current_cs = getattr(cf, "choice_set", None)
    current_cs_id = (current_cs.get("id") if isinstance(current_cs, dict) else getattr(current_cs, "id", None))
    if current_cs_id != cs_id:
        return nb.extras.custom_fields.update([{"id": cf.id, **payload}])[0]
    return cf

# ----------------------------- run setup -----------------------------
print("→ Ensuring custom fields (NetBox 4.2.9)…")
ensure_cf_text("cluster_name", "Cluster Name")
ensure_cf_select("hpc_role", "HPC Role", ["cpu", "gpu", "storage", "switch", "pdu"])
ensure_cf_int("estimated_watts", "Estimated Watts")

print("→ Ensuring tags…")
gpu_tag  = ensure_tag("gpu-node",  "gpu-node")
cpu_tag  = ensure_tag("cpu-node",  "cpu-node")
stor_tag = ensure_tag("storage",   "storage")
tor_tag  = ensure_tag("tor",       "tor")
agg_tag  = ensure_tag("agg",       "agg")
pdu_tag  = ensure_tag("pdu",       "pdu")

print("→ Ensuring roles…")
ROLE_SERVER  = ensure_device_role("Server",  "server")
ROLE_NETWORK = ensure_device_role("Network", "network")

print("→ Ensuring site/rack…")
site = get_or_create(nb.dcim.sites, name="SF-Dogpatch", slug="sf-dogpatch")
rack_role = get_or_create(nb.dcim.rack_roles, name="Compute", slug="compute")
rack = get_or_create(nb.dcim.racks, name="SFDP-Rack-01", site=site.id, role=rack_role.id, u_height=48)

print("→ Ensuring manufacturer & device types…")
mfg = ensure_manufacturer("Generic")
dt_gpu  = ensure_device_type(mfg, "GPU Node 2x100G", u=2)
dt_cpu  = ensure_device_type(mfg, "CPU Node 2x25G",  u=1)
dt_stor = ensure_device_type(mfg, "Storage Head 4x25G", u=2)
dt_tor  = ensure_device_type(mfg, "ToR Switch 32x100G", u=1)
dt_agg  = ensure_device_type(mfg, "Aggregation Switch 8x100G", u=2)
dt_pdu  = ensure_device_type(mfg, "PDU 24-outlet", u=2)

def ensure_device(name: str, dt, ru: int, tags, cf):
    dev = nb.dcim.devices.get(name=name)
    if dev:
        return dev
    model = getattr(dt, "model", "") or getattr(dt, "display", "")
    role_id = ROLE_NETWORK.id if ("Switch" in model or "PDU" in model) else ROLE_SERVER.id
    return nb.dcim.devices.create({
        "name": name, "device_type": dt.id, "role": role_id,
        "site": site.id, "rack": rack.id, "position": ru, "face": "front",
        "tags": [t.id for t in tags], "custom_fields": cf,
    })

print("→ Creating devices in rack…")
tor  = ensure_device("TOR-SFDP-01",  dt_tor,  42, [tor_tag, agg_tag], {"cluster_name":"atlas","hpc_role":"switch","estimated_watts":180})
agg  = ensure_device("AGG-SFDP-01",  dt_agg,  46, [agg_tag],          {"cluster_name":"atlas","hpc_role":"switch","estimated_watts":220})
pdu  = ensure_device("PDU-SFDP-01",  dt_pdu,   2, [pdu_tag],          {"cluster_name":"atlas","hpc_role":"pdu","estimated_watts":20})
gpu1 = ensure_device("GPU-SFDP-01",  dt_gpu,  20, [gpu_tag],          {"cluster_name":"atlas","hpc_role":"gpu","estimated_watts":800})
gpu2 = ensure_device("GPU-SFDP-02",  dt_gpu,  18, [gpu_tag],          {"cluster_name":"atlas","hpc_role":"gpu","estimated_watts":800})
cpu1 = ensure_device("CPU-SFDP-01",  dt_cpu,  16, [cpu_tag],          {"cluster_name":"atlas","hpc_role":"cpu","estimated_watts":250})
stor = ensure_device("STOR-SFDP-01", dt_stor, 14, [stor_tag],         {"cluster_name":"atlas","hpc_role":"storage","estimated_watts":350})

# ---------- interfaces & power ----------
def _normalize_iface_type(hint: str):
    """Map speed hints -> NetBox 4.x interface type slugs."""
    if not hint:
        return "other"
    s = str(hint).lower()
    if "100g" in s: return "100gbase-x-qsfp28"
    if "25g"  in s: return "25gbase-x-sfp28"
    if "40g"  in s: return "40gbase-x-qsfpp"
    if "10g"  in s: return "10gbase-x-sfpp"
    if "1g"   in s or "1000" in s: return "1000base-t"
    if "lag"  in s or "bond" in s or "port-channel" in s: return "lag"
    return "other"

def ensure_iface(device, name, iface_type_hint=None, mgmt=False):
    intf = nb.dcim.interfaces.get(device_id=device.id, name=name)
    if intf:
        return intf
    nb_type = _normalize_iface_type(iface_type_hint or name)
    return nb.dcim.interfaces.create({"device": device.id, "name": name, "type": nb_type, "mgmt_only": mgmt})

def ensure_power_port(device, name, maximum_draw=1000, allocated_draw=800):
    pp = nb.dcim.power_ports.get(device_id=device.id, name=name)
    if pp:
        return pp
    return nb.dcim.power_ports.create({"device": device.id, "name": name, "maximum_draw": maximum_draw, "allocated_draw": allocated_draw})

def ensure_power_outlet(device, name):
    po = nb.dcim.power_outlets.get(device_id=device.id, name=name)
    if po:
        return po
    return nb.dcim.power_outlets.create({"device": device.id, "name": name})

print("→ Adding interfaces & power components…")
# TOR ports (100G)
tor_ports = [ensure_iface(tor, f"xe-{i:02}", "100g") for i in range(1, 33)]
agg_upl1  = ensure_iface(tor, "uplink-1", "100g")
agg_upl2  = ensure_iface(tor, "uplink-2", "100g")
# AGG ports (100G)
agg_ports = [ensure_iface(agg, f"xe-{i:02}", "100g") for i in range(1, 9)]
# Server NICs
gpu1_p1 = ensure_iface(gpu1, "eth0-100G", "100g")
gpu1_p2 = ensure_iface(gpu1, "eth1-100G", "100g")
gpu2_p1 = ensure_iface(gpu2, "eth0-100G", "100g")
gpu2_p2 = ensure_iface(gpu2, "eth1-100G", "100g")
cpu1_p1 = ensure_iface(cpu1, "eth0-25G",  "25g")
stor_p1 = ensure_iface(stor, "eth0-25G",  "25g")
# Power ports
gpu1_pp = ensure_power_port(gpu1, "PSU1", maximum_draw=1000, allocated_draw=800)
gpu2_pp = ensure_power_port(gpu2, "PSU1", maximum_draw=1000, allocated_draw=800)
cpu1_pp = ensure_power_port(cpu1, "PSU1", maximum_draw=400,  allocated_draw=250)
stor_pp = ensure_power_port(stor, "PSU1", maximum_draw=600,  allocated_draw=350)
# PDU outlets
pdu_outlets = [ensure_power_outlet(pdu, f"OUT{n}") for n in range(1, 25)]

# --- NetBox 4.2.x cable helpers (use a_terminations / b_terminations) ---
def _has_cable(termination_obj):
    try:
        return bool(getattr(termination_obj, "cable", None))
    except Exception:
        return False

def cable_ifaces(a_iface, b_iface):
    if _has_cable(a_iface) or _has_cable(b_iface):
        return
    nb.dcim.cables.create({
        "a_terminations": [{"object_type": "dcim.interface", "object_id": a_iface.id}],
        "b_terminations": [{"object_type": "dcim.interface", "object_id": b_iface.id}],
    })

def cable_power(power_port, power_outlet):
    if _has_cable(power_port) or _has_cable(power_outlet):
        return
    nb.dcim.cables.create({
        "a_terminations": [{"object_type": "dcim.powerport",   "object_id": power_port.id}],
        "b_terminations": [{"object_type": "dcim.poweroutlet", "object_id": power_outlet.id}],
    })

print("→ Cabling data & power…")
# Servers → ToR
cable_ifaces(gpu1_p1, tor_ports[0])
cable_ifaces(gpu1_p2, tor_ports[1])
cable_ifaces(gpu2_p1, tor_ports[2])
cable_ifaces(gpu2_p2, tor_ports[3])
cable_ifaces(cpu1_p1, tor_ports[4])
cable_ifaces(stor_p1, tor_ports[5])
# ToR → Agg
cable_ifaces(agg_upl1, agg_ports[0])
cable_ifaces(agg_upl2, agg_ports[1])
# Power: PDU → device PSUs
cable_power(gpu1_pp, pdu_outlets[1])
cable_power(gpu2_pp, pdu_outlets[2])
cable_power(cpu1_pp, pdu_outlets[3])
cable_power(stor_pp, pdu_outlets[4])

print("✅ Built rack/devices/power/cabling for SF-Dogpatch.")

# ----------------------------- metrics -----------------------------
# Rack power roll-up from custom field 'estimated_watts'
devices_in_rack = list(nb.dcim.devices.filter(rack_id=rack.id))
rack_watts = 0
for d in devices_in_rack:
    cf = getattr(d, "custom_fields", {}) or {}
    rack_watts += int(cf.get("estimated_watts") or 0)
print(f"🔌 Estimated rack draw (sum of device 'estimated_watts'): {rack_watts} W")

# ToR port utilization
all_tor_ifaces = list(nb.dcim.interfaces.filter(device_id=tor.id, limit=1000))
total_ports = len(all_tor_ifaces)
used_ports = len([i for i in all_tor_ifaces if getattr(i, 'cable', None)])
util = round(100 * used_ports / total_ports, 1) if total_ports else 0.0
print(f"📊 ToR port utilization: {used_ports}/{total_ports} = {util}%")

# ----------------------------- robust trace printer -----------------------------
# ----------------------------- robust trace (raw REST) -----------------------------
import requests

def _term_to_str(term):
    if not term:
        return "-"
    if isinstance(term, dict):
        dev = term.get("device") or {}
        devname = dev.get("name") or dev.get("display") or ""
        objtype = term.get("obj_type") or term.get("type") or term.get("object_type") or "term"
        name = term.get("name") or term.get("display") or ""
        return f"{objtype}:{name}@{devname}" if devname else f"{objtype}:{name}"
    return str(term)

def print_interface_trace_raw(netbox_url, token, iface_id, title="Trace"):
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Token {token}"})
    r = sess.get(f"{netbox_url}/api/dcim/interfaces/{iface_id}/trace/", timeout=30)
    r.raise_for_status()
    tr = r.json()

    print(f"🧭 {title}")
    # Case A: list of triples [left, cable, right]
    if isinstance(tr, list) and tr and isinstance(tr[0], list):
        for hop in tr:
            if len(hop) == 3:
                left, cable, right = hop
                label = (cable or {}).get("label") if isinstance(cable, dict) else "cable"
                print(f"   {_term_to_str(left)}  --[{label}]-->  {_term_to_str(right)}")
            else:
                print(f"   {_term_to_str(hop)}")
        return

    # Case B: list of dict segments {"termination_a":..., "termination_b":..., "cable":...}
    if isinstance(tr, list) and tr and isinstance(tr[0], dict):
        for seg in tr:
            left  = seg.get("termination_a") or seg.get("a") or seg.get("from")
            right = seg.get("termination_b") or seg.get("b") or seg.get("to")
            cable = seg.get("cable") or {}
            label = cable.get("label") or cable.get("display") or "cable"
            if left or right:
                print(f"   {_term_to_str(left)}  --[{label}]-->  {_term_to_str(right)}")
            else:
                print(f"   {_term_to_str(seg)}")
        return

    # Case C: flat list (terms/cables/names) — print sequentially
    if isinstance(tr, list):
        for item in tr:
            print(f"   {_term_to_str(item)}")
        return

    # Fallback: raw dump
    print("   [raw trace]")
    try:
        import pprint
        pprint.pp(tr)
    except Exception:
        print(tr)

# ----- call it with your GPU NIC -----
dev_name = getattr(gpu1, "name", "DEVICE")
if_name  = getattr(gpu1_p1, "name", "IFACE")
print_interface_trace_raw(NETBOX_URL, NETBOX_TOKEN, gpu1_p1.id, title=f"Trace {dev_name} {if_name} → …")
