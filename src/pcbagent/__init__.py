"""PCBAgent: local, verified PCB design.

Natural-language request -> locked spec -> circuit IR -> ngspice verification
across tolerance corners -> JLCPCB parts from a local catalog -> KiCad netlist
-> constraint-driven placement -> Freerouting -> KiCad DRC. Nothing leaves the
machine.
"""

__version__ = "0.1.0"
