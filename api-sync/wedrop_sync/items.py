"""Montagem da fila de atualização: paginação, dedupe e prioridade.

Regra do produto (validada no histórico): o filtro "estoque baixo" vem do
**servidor** (`lowStock=true`), e os anúncios com estoque desatualizado
(`outdatedStock=true`) entram na frente. Isso evita paginar o catálogo inteiro
no cliente só para filtrar ≤10.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class Item:
    item_id: Any
    title: str = ""
    sku: str = ""
    outdated: bool = False
    situation: str = ""
    #: quantidade publicada hoje no marketplace (`itemsProducts[0].availableQuantity`)
    available_qty: Optional[int] = None
    #: estoque do produto na WeDrop (`itemsProducts[0].product.availableQuantity`)
    source_qty: Optional[int] = None
    #: `bloqueado` / `na_fila` / None — quando o próprio WeDrop já explica o porquê
    sync_state: Optional[str] = None
    sync_reason: Optional[str] = None
    is_variation: bool = False

    @property
    def key(self) -> str:
        return str(self.item_id)

    @property
    def blocked(self) -> bool:
        """Anúncio que o WeDrop não deixa sincronizar (combo, revisão no canal…)."""
        state = (self.sync_state or "").strip().lower()
        return state in ("bloqueado", "blocked", "erro", "error")


@dataclass
class Queue:
    items: List[Item] = field(default_factory=list)
    mapped_total: Optional[int] = None
    selected_total: Optional[int] = None
    outdated_total: Optional[int] = None
    duplicates: int = 0
    truncated: bool = False
    notes: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)


_ID_KEYS = ("id", "itemId", "item_id", "myItemId", "my_item_id", "listingId")
_TITLE_KEYS = ("title", "name", "productName", "product_name", "description")
_SKU_KEYS = ("sku", "sellerSku", "seller_sku", "code")
_OUTDATED_KEYS = ("outdatedStock", "outdated_stock", "outdated", "stockOutdated", "stock_outdated")
_SITUATION_KEYS = ("situation", "status", "listingStatus", "state")


def _first(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "sim")
    return False


def extract_items(body: Any) -> List[Dict[str, Any]]:
    """Encontra a lista de anúncios dentro do envelope da API, sem fixar o nome do campo."""
    if isinstance(body, list):
        return [entry for entry in body if isinstance(entry, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("items", "data", "results", "result", "list", "rows", "myItems", "my_items"):
        value = body.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
    return []


def extract_total(body: Any) -> Optional[int]:
    """Total de itens (quando a API informa), para o resumo de "mapeados"."""
    if not isinstance(body, dict):
        return None
    for key in ("total", "totalCount", "total_count", "count", "totalItems", "total_items"):
        value = body.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    for key in ("meta", "pagination", "pageInfo", "page_info"):
        nested = body.get(key)
        if isinstance(nested, dict):
            found = extract_total(nested)
            if found is not None:
                return found
    return None


def _quantities(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Quantidades e estado de sync que a API já devolve por anúncio.

    Observado ao vivo:
      itemsProducts[0].availableQuantity          -> o que está publicado no canal
      itemsProducts[0].product.availableQuantity  -> estoque do produto na WeDrop
      stockSyncState / stockSyncReason            -> "bloqueado", "na_fila", motivo
    """
    produtos = raw.get("itemsProducts") or raw.get("items_products") or []
    published = source = None
    if produtos and isinstance(produtos[0], dict):
        primeiro = produtos[0]
        qty = primeiro.get("availableQuantity")
        if isinstance(qty, (int, float)):
            published = int(qty)
        produto = primeiro.get("product")
        if not isinstance(produto, dict):
            produto = primeiro
        origem = produto.get("availableQuantity")
        if isinstance(origem, (int, float)) and produto is not primeiro:
            source = int(origem)
    estado = _first(raw, ("stockSyncState", "stockSync_state"))
    motivo = _first(raw, ("stockSyncReason", "stockSync_reason"))
    return {
        "available_qty": published,
        "source_qty": source,
        "sync_state": str(estado) if estado else None,
        "sync_reason": str(motivo) if motivo else None,
        "is_variation": _truthy(raw.get("isVariation")),
    }


def to_item(raw: Dict[str, Any]) -> Optional[Item]:
    item_id = _first(raw, _ID_KEYS)
    if item_id in (None, ""):
        return None
    title = _first(raw, _TITLE_KEYS)
    sku = _first(raw, _SKU_KEYS)
    situation = _first(raw, _SITUATION_KEYS)
    extra = _quantities(raw)
    return Item(
        item_id=item_id,
        title=str(title)[:120] if title else "",
        sku=str(sku) if sku else "",
        outdated=any(_truthy(raw.get(key)) for key in _OUTDATED_KEYS),
        situation=str(situation) if situation else "",
        **extra,
    )


def fetch_page(
    api,
    *,
    integration_id: int,
    user_id: Optional[int],
    limit: int,
    offset: int,
    low_stock: bool,
    outdated_only: bool = False,
    attempts: int = 3,
) -> Any:
    params = {
        "integrationId": integration_id,
        "userId": user_id,
        "situation": "mapped",
        "limit": limit,
        "offset": offset,
        "lowStock": "true" if low_stock else None,
        "outdatedStock": "true" if outdated_only else None,
    }
    response = api.get("/my-items", params=params, attempts=attempts)
    return response


def collect_bulk(
    api,
    *,
    integration_id: int,
    user_id: Optional[int],
    low_stock: bool,
    outdated_only: bool = False,
    max_items: int = 0,
    limit: int = 999999,
) -> tuple[List[Item], Optional[int], bool]:
    """Traz a seleção inteira numa chamada só — é o que o front-end faz.

    Medido na conta real: 2278 itens em ~8s (Shopee) e 2427 em ~23s (ML2).
    Uma única resposta grande é mais rápida (e mais gentil) que 46 páginas.
    """
    response = fetch_page(
        api,
        integration_id=integration_id,
        user_id=user_id,
        limit=limit,
        offset=0,
        low_stock=low_stock,
        outdated_only=outdated_only,
    )
    bruto = extract_items(response.data)
    itens: List[Item] = []
    vistos = set()
    for raw in bruto:
        item = to_item(raw)
        if item is None or item.key in vistos:
            continue
        vistos.add(item.key)
        itens.append(item)
    truncado = False
    if max_items and len(itens) > max_items:
        itens = itens[:max_items]
        truncado = True
    return itens, extract_total(response.data), truncado


def collect(
    api,
    *,
    integration_id: int,
    user_id: Optional[int],
    low_stock: bool,
    page_size: int,
    outdated_only: bool = False,
    max_items: int = 0,
    log=None,
) -> tuple[List[Item], Optional[int], bool]:
    """Pagina até acabar. Devolve (itens, total informado pela API, truncado)."""
    if page_size <= 0:
        return collect_bulk(
            api,
            integration_id=integration_id,
            user_id=user_id,
            low_stock=low_stock,
            outdated_only=outdated_only,
            max_items=max_items,
        )

    collected: List[Item] = []
    seen = set()
    offset = 0
    total_from_api: Optional[int] = None
    truncated = False

    while True:
        response = fetch_page(
            api,
            integration_id=integration_id,
            user_id=user_id,
            limit=page_size,
            offset=offset,
            low_stock=low_stock,
            outdated_only=outdated_only,
        )
        if total_from_api is None:
            total_from_api = extract_total(response.data)

        page_items = extract_items(response.data)
        if not page_items:
            break

        for raw in page_items:
            item = to_item(raw)
            if item is None or item.key in seen:
                continue
            seen.add(item.key)
            collected.append(item)

        offset += page_size
        if len(page_items) < page_size:
            break
        if max_items and len(collected) >= max_items:
            truncated = True
            break
        if log is not None:
            log("fila: %d itens lidos (offset=%d)", len(collected), offset)

    if max_items and len(collected) > max_items:
        collected = collected[:max_items]
        truncated = True
    return collected, total_from_api, truncated


def build_queue(
    api,
    *,
    integration_id: int,
    user_id: Optional[int],
    all_items: bool,
    page_size: int = 0,
    max_items: int = 0,
    log=None,
) -> Queue:
    """Fila = seleção do canal, com os desatualizados na frente e sem repetidos.

    Seleção:
      * modo normal  -> anúncios mapeados com estoque baixo (servidor filtra);
      * modo --all   -> todos os anúncios mapeados.
    Prioridade: quem já está com estoque desatualizado vai primeiro, porque é
    ali que a atualização tem efeito imediato na vitrine.

    `page_size=0` (padrão) traz tudo numa chamada; um valor >0 força paginação.
    """
    queue = Queue()

    selected, selected_total, truncated = collect(
        api,
        integration_id=integration_id,
        user_id=user_id,
        low_stock=not all_items,
        page_size=page_size,
        max_items=max_items,
        log=log,
    )
    queue.selected_total = selected_total if selected_total is not None else len(selected)
    queue.truncated = truncated
    if all_items:
        queue.notes.append("modo completo: todos os anúncios mapeados")

    outdated, outdated_total, _ = collect(
        api,
        integration_id=integration_id,
        user_id=user_id,
        low_stock=not all_items,
        page_size=page_size,
        outdated_only=True,
        max_items=max_items,
        log=log,
    )
    queue.outdated_total = outdated_total if outdated_total is not None else len(outdated)

    outdated_keys = {item.key for item in outdated}
    ordered: List[Item] = []
    seen = set()
    for item in outdated:
        if item.key in seen:
            queue.duplicates += 1
            continue
        seen.add(item.key)
        # marca como desatualizado preservando quantidades/estado de sync
        ordered.append(
            Item(
                item_id=item.item_id,
                title=item.title,
                sku=item.sku,
                outdated=True,
                situation=item.situation,
                available_qty=item.available_qty,
                source_qty=item.source_qty,
                sync_state=item.sync_state,
                sync_reason=item.sync_reason,
                is_variation=item.is_variation,
            )
        )
    for item in selected:
        if item.key in seen:
            queue.duplicates += 1
            continue
        seen.add(item.key)
        ordered.append(item)

    # o limite vale para a FILA final (seleção + desatualizados somam mais itens)
    if max_items and len(ordered) > max_items:
        ordered = ordered[:max_items]
        queue.truncated = True
        queue.notes.append(f"limitado a {max_items} anúncio(s) por --max")

    queue.items = ordered
    # "mapeados" do SUMMARY é o tamanho do catálogo do canal, não o da seleção:
    # uma requisição de 1 item sem filtro já traz o total informado pela API.
    if all_items:
        queue.mapped_total = queue.selected_total
    else:
        try:
            probe = fetch_page(
                api,
                integration_id=integration_id,
                user_id=user_id,
                limit=1,
                offset=0,
                low_stock=False,
            )
            mapped = extract_total(probe.data)
        except Exception:  # leitura de cortesia: se falhar, não estraga a rodada
            mapped = None
        queue.mapped_total = mapped if mapped is not None else (queue.selected_total or len(selected))
    return queue
