import json
import sys
from pathlib import Path

from meiva_builtins.plugins.meiva_memory.template import validate

print(json.dumps([validate.note_char_cap(), validate.validate_note(Path(sys.argv[1]))]))
