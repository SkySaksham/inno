"""
Inno metadata schema.

metadata/
├── index.json
├── call_graph.json
└── functions/
    └── <file>.json
"""


# metadata/index.json
INDEX = {
    "indexed_sha": "git commit SHA",
    "files": {
        "src/example.py": {
            "blob_hash": "git blob hash",
            "functions": ["foo", "bar"]
        }
    }
}


# metadata/functions/<file>.json
FUNCTION = {
    "foo": {
        "file": "src/example.py",
        "line_start": 10,
        "line_end": 20,

        "params": [
            {
                "name": "x",
                "type": "int"
            }
        ],

        "returns": "str",

        "body_hash": "hash of function body",

        "calls": ["bar"],

        "summary": "Converts x into a string.",

        "type_confidence": "declared"  # or "inferred"
    }
}


# metadata/call_graph.json
CALL_GRAPH = {
    "foo": {
        "calls": ["bar"],
        "called_by": ["main"]
    },

    "bar": {
        "calls": [],
        "called_by": ["foo"]
    }
}


"""
Quick interpretation:

index.json
    -> which files/functions were indexed

functions/*.json
    -> detailed metadata for each function

call_graph.json
    -> relationships between functions

type_confidence:
    declared = type came from Python code
    inferred = type was inferred by the LLM

body_hash:
    used to detect whether a function changed

summary:
    short description of what the function does
"""