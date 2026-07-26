#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""
Quick validation script for skills - minimal version

PyYAML is not in the container's base interpreter, so this declares it inline
(PEP 723) and is meant to be run as `uv run --script quick_validate.py`. Run
with a bare `python3` it dies on `import yaml` before doing any work — which is
how it silently failed as a gate: the traceback looked like the *skill* was
broken, not the validator.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / 'SKILL.md'
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter
    content = skill_md.read_text()
    if not content.startswith('---'):
        return False, "No YAML frontmatter found"

    # Extract frontmatter
    match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Define allowed properties.
    #
    # `model`, `effort` and `when_to_use` are multiplai conventions on top of
    # the upstream skill spec. They were missing here, which meant this
    # validator rejected every skill in the marketplace — all 43 of them —
    # with "Unexpected key(s): effort, model, when_to_use". Anything built on
    # top of it (the promote_skill gate) would have inherited that.
    ALLOWED_PROPERTIES = {
        'name', 'description', 'license', 'allowed-tools', 'metadata',
        'model', 'effort', 'when_to_use', 'disable-model-invocation',
    }

    # Frontmatter values Claude Code actually accepts. A typo is ignored at
    # runtime, so the skill silently runs on the wrong tier.
    KNOWN_MODELS = {'opus', 'sonnet', 'haiku', 'inherit', 'fable'}
    KNOWN_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}

    # Check for unexpected properties (excluding nested keys under metadata)
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected_keys))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    model = frontmatter.get('model')
    if model is not None and model not in KNOWN_MODELS:
        return False, (
            f"Unknown model '{model}'. Known models: {', '.join(sorted(KNOWN_MODELS))}"
        )

    effort = frontmatter.get('effort')
    if effort is not None and effort not in KNOWN_EFFORTS:
        return False, (
            f"Unknown effort '{effort}'. Known efforts: {', '.join(sorted(KNOWN_EFFORTS))}"
        )

    # Check required fields
    if 'name' not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if 'description' not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    # Extract name for validation
    name = frontmatter.get('name', '')
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        # Check naming convention (hyphen-case: lowercase with hyphens)
        if not re.match(r'^[a-z0-9-]+$', name):
            return False, f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)"
        if name.startswith('-') or name.endswith('-') or '--' in name:
            return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
        # Check name length (max 64 characters per spec)
        if len(name) > 64:
            return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters."

    # Extract and validate description
    description = frontmatter.get('description', '')
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if description:
        # Check for angle brackets
        if '<' in description or '>' in description:
            return False, "Description cannot contain angle brackets (< or >)"
        # Check description length (max 1024 characters per spec)
        if len(description) > 1024:
            return False, f"Description is too long ({len(description)} characters). Maximum is 1024 characters."

    return True, "Skill is valid!"

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a skill directory's SKILL.md frontmatter.")
    parser.add_argument("skill_directory",
                        help="path to the skill directory containing SKILL.md")
    args = parser.parse_args(argv)

    valid, message = validate_skill(args.skill_directory)
    print(message)
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())