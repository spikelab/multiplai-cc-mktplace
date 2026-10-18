"""code_facts: checked, cited facts about what code does.

Shared by codebase-walkthrough (a whole module), review (a diff) and buildme.
The caller passes in its model call function and its path resolver; this
package owns the fact models, the slicing into units, the explore prompt and
the gating of the answers. `map_paths` / `write_paths_file` produce the paths.v1
document: the code paths from each entry point of a snapshot.
"""

from .entries import discover_entries
from .explore import (UNRESOLVED, CallFn, Gated, Resolve, ask, bounded, check_citation, citation_keeper,
                      gate_answers, reask_units, run_tasks, unit_key, ws_path)
from .models import Citation, Dropped, ExploreOutput, Fact, FilePurpose, Scope, Side, Span, Term, Unit
from .partition import partition
from .paths import (E2EListing, E2ETest, Location, MockedE2ETest, PathDrop, PathEntry, PathHop, PathsResult,
                    Seed, TracedPath, map_paths, write_paths_file)
from .prompts import PATH_RULE, UNTRUSTED_RULES, citation_rules, explore_unit, schema_json

__all__ = [
    "CallFn", "Citation", "Dropped", "E2EListing", "E2ETest", "ExploreOutput", "Fact", "FilePurpose", "Gated",
    "Location", "MockedE2ETest", "PATH_RULE", "PathDrop", "PathEntry", "PathHop", "PathsResult", "Resolve",
    "Scope", "Seed", "Side", "Span", "Term", "TracedPath", "UNRESOLVED", "UNTRUSTED_RULES", "Unit", "ask",
    "bounded", "check_citation", "citation_keeper", "citation_rules", "discover_entries", "explore_unit",
    "gate_answers", "map_paths", "partition", "reask_units", "run_tasks", "schema_json", "unit_key",
    "write_paths_file", "ws_path",
]
