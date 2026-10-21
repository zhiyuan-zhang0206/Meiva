# meiva:note
# Read one ``provider.py`` module's model and price declarations without executing it. The
# pricing workflow may resume an existing bot branch, so executing that branch's code would
# cross the workflow's trusted-main boundary. A declaration participates when its constructor
# is imported from the provider contract, under whatever local name the module gives it —
# never because of a class name: matching ``ProviderContribution`` literally is what broke the
# nightly sync for ten days once a plugin switched to ``DecisionProviderContribution`` (task
# #5496). Decision models are not chat models, but their prices land in the same ``PriceBook``
# that bills their calls and the archive carries their ids, so a decision declaration left out
# would freeze its rates silently. Every declaration in the file participates, not just one
# named ``PROVIDER``: ``lm_openai`` declares ``DECISION_PROVIDER`` beside it.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md

from __future__ import annotations

import ast
from collections.abc import Mapping, Set
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple, cast

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


class ProviderManifest(NamedTuple):
    models: frozenset[str]
    prices: dict[str, PriceDeclaration]
    price_nodes: dict[str, ast.Call]
    declarations: tuple[str, ...]


# meiva:note
# The provider contract a ``provider.py`` imports its contribution types from: ``base/lm/provider_api.py``
# is the plugin-facing module and ``base/lm/catalog/provider_contract.py`` owns the dataclasses. A
# declaration is recognized by that origin; a class name proves nothing about where it came from.
_CONTRACT_MODULES = frozenset({"base.lm.provider_api", "base.lm.catalog.provider_contract"})


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
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


def _string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[statement.targets[0].id] = value.value
    return constants


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


def _literal(node: ast.expr, expected: type[Any], *, context: str) -> Any:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a literal") from exc
    if not isinstance(value, expected):
        raise TypeError(f"{context} must be a {expected.__name__} literal")
    return value


def _optional_string_literal(node: ast.expr, *, context: str) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a string or None literal") from exc
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{context} must be a string or None literal")
    return value


def _integer_literal(node: ast.expr, *, context: str) -> int:
    value = _literal(node, int, context=context)
    if isinstance(value, bool):
        raise TypeError(f"{context} must be an integer literal")
    return cast(int, value)


def _optional_integer_literal(node: ast.expr, *, context: str) -> int | None:
    try:
        value = ast.literal_eval(node)
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
    if any(keyword.arg is None for keyword in node.keywords):
        raise RuntimeError(f"{context} cannot contain keyword expansion")
    fields = {cast(str, keyword.arg): keyword.value for keyword in node.keywords}
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
        optional={"cache_write_5m", "cache_write_1h"},
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
        optional={"windows", "cache_write_5m", "cache_write_1h"},
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
        optional={"vendor", "periods", "cache_write_5m", "cache_write_1h"},
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


def _mapping_keyword(call: ast.Call, name: str, *, path: Path, declaration: str) -> ast.Dict:
    values = [keyword.value for keyword in call.keywords if keyword.arg == name]
    if len(values) != 1 or not isinstance(values[0], ast.Dict):
        raise RuntimeError(f"{path}: {declaration}(...) must have one literal {name}= mapping")
    return values[0]


def _mapping_keys(
    mapping: ast.Dict, *, context: str, constants: Mapping[str, str]
) -> tuple[str, ...]:
    if len(mapping.keys) != len(mapping.values) or any(key is None for key in mapping.keys):
        raise RuntimeError(f"{context} cannot contain mapping expansion")
    keys = tuple(
        _model_key(cast(ast.expr, key), constants=constants, context=f"{context} key")
        for key in mapping.keys
    )
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"{context} contains duplicate model ids")
    return keys


def _model_key(node: ast.expr, *, constants: Mapping[str, str], context: str) -> str:
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return cast(str, _literal(node, str, context=context))


def _fast_variant_calls(tree: ast.Module) -> list[ast.Call]:
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "contribute"
    ]
    return [
        node
        for function in functions
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "with_fast_variants"
    ]


def _fast_price_nodes(
    tree: ast.Module,
    path: Path,
    declarations: Mapping[str, ast.Call],
    constants: Mapping[str, str],
) -> dict[str, dict[str, ast.expr]]:
    calls = _fast_variant_calls(tree)
    if not calls:
        return {}
    if len(calls) != 1:
        raise RuntimeError(f"{path}: expected at most one with_fast_variants declaration")
    call = calls[0]
    if (
        len(call.args) != 2
        or any(
            keyword.arg != "reference_tps" or not isinstance(keyword.value, ast.Dict)
            for keyword in call.keywords
        )
        or not isinstance(call.args[0], ast.Name)
        or call.args[0].id not in declarations
        or not isinstance(call.args[1], ast.Dict)
    ):
        raise RuntimeError(f"{path}: expected literal with_fast_variants(<contribution>, {{...}})")
    mapping = call.args[1]
    keys = _mapping_keys(mapping, context=f"{path}: Fast pricing", constants=constants)
    return {call.args[0].id: dict(zip(keys, mapping.values, strict=True))}


def _add_fast_prices(
    nodes: dict[str, ast.expr],
    path: Path,
    models: set[str],
    prices: dict[str, PriceDeclaration],
    price_nodes: dict[str, ast.Call],
) -> None:
    for standard, value in nodes.items():
        fast = f"{standard}-fast"
        if standard not in models or fast in models or fast in prices:
            raise RuntimeError(f"{path}: invalid or duplicate Fast model {fast!r}")
        models.add(fast)
        prices[fast], price_nodes[fast] = _price_declaration(value, context=f"{path}: {fast}")


def provider_manifest(source: str, path: Path) -> ProviderManifest:
    tree = ast.parse(source, filename=str(path))
    bindings = _import_bindings(tree)
    constants = _string_constants(tree)
    declarations: dict[str, ast.Call] = {}
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Call)
        ):
            continue
        if _contract_symbol(statement.value.func, bindings) is not None:
            declarations[statement.targets[0].id] = statement.value
    if not declarations:
        raise RuntimeError(
            f"{path}: expected a top-level <contribution> = ...(models=..., pricing=...) whose "
            f"type is imported from {' or '.join(sorted(_CONTRACT_MODULES))}"
        )
    models: set[str] = set()
    prices: dict[str, PriceDeclaration] = {}
    price_nodes: dict[str, ast.Call] = {}
    for name, call in declarations.items():
        models_node = _mapping_keyword(call, "models", path=path, declaration=name)
        pricing_node = _mapping_keyword(call, "pricing", path=path, declaration=name)
        declared = set(
            _mapping_keys(models_node, context=f"{path}: {name} models", constants=constants)
        )
        price_models = _mapping_keys(
            pricing_node, context=f"{path}: {name} pricing", constants=constants
        )
        if not set(price_models) <= declared:
            raise RuntimeError(f"{path}: {name} pricing contains ids absent from models")
        repeated = prices.keys() & set(price_models)
        if repeated:
            raise RuntimeError(f"{path}: {name} repeats model ids {sorted(repeated)}")
        models |= declared
        for model, value in zip(price_models, pricing_node.values, strict=True):
            prices[model], price_nodes[model] = _price_declaration(
                value,
                context=f"{path}: {model}",
            )
    for nodes in _fast_price_nodes(tree, path, declarations, constants).values():
        _add_fast_prices(nodes, path, models, prices, price_nodes)
    return ProviderManifest(frozenset(models), prices, price_nodes, tuple(declarations))
