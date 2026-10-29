# meiva:note
# Read one provider's model and price declarations without executing it. The pricing workflow
# may resume an existing bot branch, so executing that branch's code would cross the workflow's
# trusted-main boundary. A declaration participates when its constructor is imported from the
# provider contract, under whatever local name the module gives it — never because of a class
# name: matching ``ProviderContribution`` literally is what broke the nightly sync for ten days
# once a plugin switched to ``DecisionProviderContribution`` (task #5496). Decision models are
# not chat models, but their prices land in the same ``PriceBook`` that bills their calls and
# the archive carries their ids, so a decision declaration left out would freeze its rates
# silently. Every declaration in the unit participates, not just one named ``PROVIDER``:
# ``lm_openai`` declares ``DECISION_PROVIDER`` beside it.
#
# The unit is the provider package, not one file. A declaration may name a literal another
# module of the same package assigns (``models=MODELS``, ``with_fast_variants(PROVIDER,
# FAST_PRICES)``), so the reader parses every source of the package and follows a module-level
# name to the literal it names. Nothing is executed either way.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md

from __future__ import annotations

import ast
from collections.abc import Mapping, Set
from datetime import date
from pathlib import Path
from typing import NamedTuple

from scripts.model_registry.plugin_price_rates import FlatRates, rates_from_nodes


class WindowDeclaration(NamedTuple):
    start: str
    end: str
    rates: FlatRates


class TierDeclaration(NamedTuple):
    input_tokens_min: int
    input_tokens_max: int | None
    rates: FlatRates
    windows: tuple[WindowDeclaration, ...]


class PeriodDeclaration(NamedTuple):
    effective_from: str | None
    effective_until: str | None
    tiers: tuple[TierDeclaration, ...]


class PriceDeclaration(NamedTuple):
    rates: FlatRates
    source_url: str
    source_checked_at: str
    vendor: str | None
    periods: tuple[PeriodDeclaration, ...]


class SourceNode(NamedTuple):
    path: Path
    node: ast.Call


class ProviderManifest(NamedTuple):
    models: frozenset[str]
    prices: dict[str, PriceDeclaration]
    price_nodes: dict[str, SourceNode]
    declarations: tuple[str, ...]
    sources: Mapping[Path, str]


# meiva:note
# The provider contract a provider source imports its contribution types from:
# ``base/lm/provider_api.py`` is the plugin-facing module and
# ``base/lm/catalog/provider_contract.py`` owns the dataclasses. A declaration is recognized by
# that origin; a class name proves nothing about where it came from.
_CONTRACT_MODULES = frozenset({"base.lm.provider_api", "base.lm.catalog.provider_contract"})


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module:
            # meiva:note
            # A relative import keeps only its tail: `from .prices import PRICES` binds
            # "prices.PRICES", which the unit resolves against its sibling modules.
            for alias in statement.names:
                bindings[alias.asname or alias.name] = f"{statement.module}.{alias.name}"
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                local = alias.asname or alias.name.split(".", maxsplit=1)[0]
                bindings[local] = alias.name if alias.asname else local
    return bindings


def imported_names(source: str, path: Path) -> set[str]:
    return set(_import_bindings(ast.parse(source, filename=str(path))))


def _dotted_name(node: ast.expr, bindings: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _dotted_name(node.value, bindings)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def _contract_symbol(node: ast.expr, bindings: Mapping[str, str]) -> str | None:
    dotted = _dotted_name(node, bindings)
    if dotted is None:
        return None
    module, _, symbol = dotted.rpartition(".")
    return symbol if module in _CONTRACT_MODULES else None


def _assignments(tree: ast.Module) -> dict[str, ast.expr]:
    bound: dict[str, ast.expr] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                bound[target.id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            bound[statement.target.id] = statement.value
    return bound


def _string_constants(assignments: Mapping[str, ast.expr]) -> dict[str, str]:
    constants: dict[str, str] = {}
    for name, value in assignments.items():
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[name] = value.value
    return constants


class _Source(NamedTuple):
    path: Path
    tree: ast.Module
    bindings: Mapping[str, str]
    assignments: Mapping[str, ast.expr]
    constants: Mapping[str, str]


class _Declaration(NamedTuple):
    name: str
    source: _Source
    call: ast.Call


# meiva:note
# One provider is one declaration unit: a declaration may name a literal another module of the
# same package assigns (``models=MODELS``, ``with_fast_variants(PROVIDER, FAST_PRICES)``), so the
# reader resolves a name through a module-level assignment in the same file or in a sibling
# module the package names. A name bound anywhere else is left alone, and the declaration that
# uses it fails its literal check instead of reading another package's module.
class _Unit:
    def __init__(self, files: tuple[_Source, ...]) -> None:
        self.files = files
        self.package = files[0].path.parent.name
        self.label = files[0].path
        self.declarations = tuple(
            _Declaration(name, source, value)
            for source in files
            for name, value in _declarations_of(source).items()
        )

    def sibling(self, module: str) -> _Source | None:
        for source in self.files:
            stem = source.path.stem
            if module == stem or module.endswith(f".{self.package}.{stem}"):
                return source
        return None

    def resolve(self, node: ast.expr, origin: _Source) -> tuple[ast.expr, _Source]:
        source = origin
        seen: set[tuple[Path, str]] = set()
        while isinstance(node, ast.Name):
            if (source.path, node.id) in seen:
                raise RuntimeError(f"{source.path}: {node.id} is defined in terms of itself")
            seen.add((source.path, node.id))
            assigned = source.assignments.get(node.id)
            if assigned is not None:
                node = assigned
                continue
            dotted = source.bindings.get(node.id)
            module, _, symbol = ("" if dotted is None else dotted).rpartition(".")
            sibling = None if not module else self.sibling(module)
            assigned = None if sibling is None else sibling.assignments.get(symbol)
            if sibling is None or assigned is None:
                return node, source
            source = sibling
            node = assigned
        return node, source

    def declaration_at(self, node: ast.expr) -> _Declaration | None:
        return next((declared for declared in self.declarations if declared.call is node), None)


def _declarations_of(source: _Source) -> dict[str, ast.Call]:
    declared: dict[str, ast.Call] = {}
    for name, value in source.assignments.items():
        if (
            isinstance(value, ast.Call)
            and _contract_symbol(value.func, source.bindings) is not None
        ):
            declared[name] = value
    return declared


# meiva:note
# The provider face is read first so every report lists its declarations before the ones a
# sibling module adds.
def _parse_unit(sources: Mapping[Path, str]) -> _Unit:
    paths = sorted(sources, key=lambda path: (path.stem != "provider", path.name))
    directories = sorted({str(path.parent) for path in paths})
    if len(directories) != 1:
        raise RuntimeError(
            "a declaration unit is one provider package; got " + ", ".join(directories)
        )
    files: list[_Source] = []
    for path in paths:
        tree = ast.parse(sources[path], filename=str(path))
        bound = _assignments(tree)
        files.append(
            _Source(
                path=path,
                tree=tree,
                bindings=_import_bindings(tree),
                assignments=bound,
                constants=_string_constants(bound),
            )
        )
    return _Unit(tuple(files))


def validated_provenance(
    source_url: object,
    source_checked_at: object,
    vendor: object,
    *,
    context: str,
) -> tuple[str, str, str | None]:
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise RuntimeError(f"{context} source_url must be an HTTPS string")
    if not isinstance(source_checked_at, str):
        raise TypeError(f"{context} source_checked_at must be a YYYY-MM-DD string")
    try:
        checked_at = date.fromisoformat(source_checked_at)
    except ValueError as exc:
        raise RuntimeError(f"{context} source_checked_at must be YYYY-MM-DD") from exc
    if checked_at.isoformat() != source_checked_at:
        raise RuntimeError(f"{context} source_checked_at must be YYYY-MM-DD")
    if vendor is not None and not isinstance(vendor, str):
        raise RuntimeError(f"{context} vendor must be a string or None")
    return source_url, source_checked_at, vendor


def _literal[T](node: ast.expr, expected: type[T], *, context: str) -> T:
    try:
        value: object = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a literal") from exc
    if not isinstance(value, expected):
        raise TypeError(f"{context} must be a {expected.__name__} literal")
    return value


def _optional_string_literal(node: ast.expr, *, context: str) -> str | None:
    try:
        value: object = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a string or None literal") from exc
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{context} must be a string or None literal")
    return value


def _integer_literal(node: ast.expr, *, context: str) -> int:
    value = _literal(node, int, context=context)
    if isinstance(value, bool):
        raise TypeError(f"{context} must be an integer literal")
    return value


def _optional_integer_literal(node: ast.expr, *, context: str) -> int | None:
    try:
        value: object = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be an integer or None literal") from exc
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{context} must be an integer or None literal")
    return value


def _call_fields(
    node: ast.expr,
    constructor: str,
    *,
    required: set[str],
    optional: Set[str] = frozenset(),
    context: str,
) -> tuple[ast.Call, dict[str, ast.expr]]:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == constructor
        and not node.args
    ):
        raise RuntimeError(f"{context} must be a literal {constructor}(...) call")
    fields: dict[str, ast.expr] = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            raise RuntimeError(f"{context} cannot contain keyword expansion")
        fields[keyword.arg] = keyword.value
    if len(fields) != len(node.keywords):
        raise RuntimeError(f"{context} contains duplicate {constructor} fields")
    missing = required - fields.keys()
    unexpected = fields.keys() - required - optional
    if missing:
        raise RuntimeError(f"{context} is missing {constructor} fields {sorted(missing)}")
    if unexpected:
        raise RuntimeError(f"{context} has unexpected {constructor} fields {sorted(unexpected)}")
    return node, fields


def _tuple_items(node: ast.expr, *, context: str) -> tuple[ast.expr, ...]:
    if not isinstance(node, ast.Tuple):
        raise TypeError(f"{context} must be a tuple literal")
    return tuple(node.elts)


def _window_declaration(node: ast.expr, *, context: str) -> WindowDeclaration:
    _call, fields = _call_fields(
        node,
        "PriceWindow",
        required={"start", "end", "cache_miss", "cache_hit", "output"},
        optional={"cache_write_5m", "cache_write_1h", "cache_write_30m"},
        context=context,
    )
    return WindowDeclaration(
        start=_literal(fields["start"], str, context=f"{context} start"),
        end=_literal(fields["end"], str, context=f"{context} end"),
        rates=rates_from_nodes(fields, context=context),
    )


def _tier_declaration(node: ast.expr, *, context: str) -> TierDeclaration:
    _call, fields = _call_fields(
        node,
        "PriceTier",
        required={"input_tokens_min", "input_tokens_max", "cache_miss", "cache_hit", "output"},
        optional={"windows", "cache_write_5m", "cache_write_1h", "cache_write_30m"},
        context=context,
    )
    window_nodes = (
        _tuple_items(fields["windows"], context=f"{context} windows") if "windows" in fields else ()
    )
    windows = tuple(
        _window_declaration(item, context=f"{context} window {index}")
        for index, item in enumerate(window_nodes)
    )
    return TierDeclaration(
        input_tokens_min=_integer_literal(
            fields["input_tokens_min"], context=f"{context} input_tokens_min"
        ),
        input_tokens_max=_optional_integer_literal(
            fields["input_tokens_max"], context=f"{context} input_tokens_max"
        ),
        rates=rates_from_nodes(fields, context=context),
        windows=windows,
    )


def _period_declaration(node: ast.expr, *, context: str) -> PeriodDeclaration:
    _call, fields = _call_fields(
        node,
        "PricePeriod",
        required={"effective_from", "effective_until", "tiers"},
        context=context,
    )
    return PeriodDeclaration(
        effective_from=_optional_string_literal(
            fields["effective_from"], context=f"{context} effective_from"
        ),
        effective_until=_optional_string_literal(
            fields["effective_until"], context=f"{context} effective_until"
        ),
        tiers=tuple(
            _tier_declaration(item, context=f"{context} tier {index}")
            for index, item in enumerate(_tuple_items(fields["tiers"], context=f"{context} tiers"))
        ),
    )


# meiva:note
# Drop a lone period that only repeats the flat rates: ``plugin_model_price`` materializes
# exactly this period from a flat declaration, so the two spellings describe one price.
# Comparing them literally would report drift against a provider that already agrees.
def collapse_flat_periods(
    periods: tuple[PeriodDeclaration, ...], rates: FlatRates
) -> tuple[PeriodDeclaration, ...]:
    if len(periods) != 1:
        return periods
    period = periods[0]
    if period.effective_from is not None or period.effective_until is not None:
        return periods
    if len(period.tiers) != 1:
        return periods
    tier = period.tiers[0]
    if (
        tier.input_tokens_min != 0
        or tier.input_tokens_max is not None
        or tier.windows
        or tier.rates != rates
    ):
        return periods
    return ()


def _price_declaration(call: ast.expr, *, context: str) -> tuple[PriceDeclaration, ast.Call]:
    price_call, fields = _call_fields(
        call,
        "PriceRates",
        required={"cache_miss", "cache_hit", "output", "source_url", "source_checked_at"},
        optional={"vendor", "periods", "cache_write_5m", "cache_write_1h", "cache_write_30m"},
        context=context,
    )
    source_url = _literal(fields["source_url"], str, context=f"{context} source_url")
    checked_at = _literal(fields["source_checked_at"], str, context=f"{context} source_checked_at")
    vendor = (
        None
        if "vendor" not in fields
        else _optional_string_literal(fields["vendor"], context=f"{context} vendor")
    )
    source_url, checked_at, vendor = validated_provenance(
        source_url,
        checked_at,
        vendor,
        context=context,
    )
    period_nodes = (
        _tuple_items(fields["periods"], context=f"{context} periods") if "periods" in fields else ()
    )
    periods = tuple(
        _period_declaration(item, context=f"{context} period {index}")
        for index, item in enumerate(period_nodes)
    )
    rates = rates_from_nodes(fields, context=context)
    return (
        PriceDeclaration(
            rates=rates,
            source_url=source_url,
            source_checked_at=checked_at,
            vendor=vendor,
            periods=collapse_flat_periods(periods, rates),
        ),
        price_call,
    )


def _mapping_keyword(unit: _Unit, declaration: _Declaration, name: str) -> tuple[ast.Dict, _Source]:
    context = f"{unit.label}: {declaration.name}"
    values = [keyword.value for keyword in declaration.call.keywords if keyword.arg == name]
    if len(values) != 1:
        raise RuntimeError(f"{context}(...) must have one literal {name}= mapping")
    node, source = unit.resolve(values[0], declaration.source)
    if isinstance(node, ast.Dict):
        return node, source
    raise RuntimeError(f"{context}(...) must have one literal {name}= mapping")


def _mapping_keys(
    mapping: ast.Dict, *, context: str, constants: Mapping[str, str]
) -> tuple[str, ...]:
    if len(mapping.keys) != len(mapping.values):
        raise RuntimeError(f"{context} cannot contain mapping expansion")
    parsed_keys: list[str] = []
    for key in mapping.keys:
        if key is None:
            raise RuntimeError(f"{context} cannot contain mapping expansion")
        parsed_keys.append(_model_key(key, constants=constants, context=f"{context} key"))
    keys = tuple(parsed_keys)
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"{context} contains duplicate model ids")
    return keys


def _model_key(node: ast.expr, *, constants: Mapping[str, str], context: str) -> str:
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return _literal(node, str, context=context)


def _fast_variant_calls(unit: _Unit) -> list[tuple[ast.Call, _Source]]:
    calls: list[tuple[ast.Call, _Source]] = []
    for source in unit.files:
        for statement in source.tree.body:
            if not (isinstance(statement, ast.FunctionDef) and statement.name == "contribute"):
                continue
            calls.extend(
                (node, source)
                for node in ast.walk(statement)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "with_fast_variants"
            )
    return calls


def _fast_variant_declaration(
    unit: _Unit, call: ast.Call, origin: _Source
) -> tuple[ast.Dict, _Source]:
    message = f"{unit.label}: expected literal with_fast_variants(<contribution>, {{...}})"
    if len(call.args) != 2 or any(keyword.arg != "reference_tps" for keyword in call.keywords):
        raise RuntimeError(message)
    references = [unit.resolve(keyword.value, origin)[0] for keyword in call.keywords]
    standard = unit.declaration_at(unit.resolve(call.args[0], origin)[0])
    mapping, source = unit.resolve(call.args[1], origin)
    if (
        standard is None
        or not isinstance(mapping, ast.Dict)
        or not all(isinstance(reference, ast.Dict) for reference in references)
    ):
        raise RuntimeError(message)
    return mapping, source


def _fast_prices(
    unit: _Unit,
    models: set[str],
    prices: dict[str, PriceDeclaration],
    price_nodes: dict[str, SourceNode],
) -> None:
    calls = _fast_variant_calls(unit)
    if not calls:
        return
    if len(calls) != 1:
        raise RuntimeError(f"{unit.label}: expected at most one with_fast_variants declaration")
    mapping, source = _fast_variant_declaration(unit, *calls[0])
    keys = _mapping_keys(mapping, context=f"{unit.label}: Fast pricing", constants=source.constants)
    for model, value in zip(keys, mapping.values, strict=True):
        node, origin = unit.resolve(value, source)
        _add_fast_price(unit, model, node, origin, models, prices, price_nodes)


def _add_fast_price(
    unit: _Unit,
    model: str,
    node: ast.expr,
    origin: _Source,
    models: set[str],
    prices: dict[str, PriceDeclaration],
    price_nodes: dict[str, SourceNode],
) -> None:
    fast = f"{model}-fast"
    if model not in models or fast in models or fast in prices:
        raise RuntimeError(f"{unit.label}: invalid or duplicate Fast model {fast!r}")
    prices[fast], price_node = _price_declaration(node, context=f"{origin.path}: {fast}")
    price_nodes[fast] = SourceNode(origin.path, price_node)
    models.add(fast)


def unit_manifest(sources: Mapping[Path, str]) -> ProviderManifest:
    unit = _parse_unit(sources)
    if not unit.declarations:
        raise RuntimeError(
            f"{unit.label}: expected a top-level <contribution> = ...(models=..., pricing=...) "
            "whose type is imported from " + " or ".join(sorted(_CONTRACT_MODULES))
        )
    models: set[str] = set()
    prices: dict[str, PriceDeclaration] = {}
    price_nodes: dict[str, SourceNode] = {}
    for declaration in unit.declarations:
        models_node, models_source = _mapping_keyword(unit, declaration, "models")
        pricing_node, pricing_source = _mapping_keyword(unit, declaration, "pricing")
        declared = set(
            _mapping_keys(
                models_node,
                context=f"{unit.label}: {declaration.name} models",
                constants=models_source.constants,
            )
        )
        price_models = _mapping_keys(
            pricing_node,
            context=f"{unit.label}: {declaration.name} pricing",
            constants=pricing_source.constants,
        )
        if not set(price_models) <= declared:
            raise RuntimeError(
                f"{unit.label}: {declaration.name} pricing contains ids absent from models"
            )
        repeated = prices.keys() & set(price_models)
        if repeated:
            raise RuntimeError(
                f"{unit.label}: {declaration.name} repeats model ids {sorted(repeated)}"
            )
        models |= declared
        for model, value in zip(price_models, pricing_node.values, strict=True):
            node, origin = unit.resolve(value, pricing_source)
            prices[model], price_node = _price_declaration(node, context=f"{origin.path}: {model}")
            price_nodes[model] = SourceNode(origin.path, price_node)
    _fast_prices(unit, models, prices, price_nodes)
    return ProviderManifest(
        frozenset(models),
        prices,
        price_nodes,
        tuple(declaration.name for declaration in unit.declarations),
        dict(sources),
    )


# meiva:note
# One file that carries a whole provider's declarations by itself; a package that spreads them
# across sibling modules is read with ``unit_manifest``.
def provider_manifest(source: str, path: Path) -> ProviderManifest:
    return unit_manifest({path: source})
