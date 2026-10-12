"""code_facts: checked, cited facts about what code does.

Shared by codebase-walkthrough (a whole module), review (a diff) and buildme.
The caller passes in its model call function and its path resolver; this
package owns the fact models, the slicing into units, the explore prompt and
the gating of the answers.
"""

from .explore import (UNRESOLVED, CallFn, Gated, Resolve, ask, bounded, check_citation, citation_keeper,
                      gate_answers, reask_units, run_tasks, unit_key, ws_path)
from .models import Citation, Dropped, ExploreOutput, Fact, FilePurpose, Scope, Side, Span, Term, Unit
from .partition import partition
from .prompts import PATH_RULE, UNTRUSTED_RULES, citation_rules, explore_unit, schema_json

__all__ = [
    "CallFn", "Citation", "Dropped", "ExploreOutput", "Fact", "FilePurpose", "Gated", "PATH_RULE", "Resolve",
    "Scope", "Side", "Span", "Term", "UNRESOLVED", "UNTRUSTED_RULES", "Unit", "ask", "bounded",
    "check_citation", "citation_keeper", "citation_rules", "explore_unit", "gate_answers", "partition",
    "reask_units", "run_tasks", "schema_json", "unit_key", "ws_path",
]
