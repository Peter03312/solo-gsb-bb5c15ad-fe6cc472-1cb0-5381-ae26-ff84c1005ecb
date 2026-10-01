"""统一业务错误：携带稳定错误码，映射为 4xx 响应。"""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, details=None):  # noqa: ANN001
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def batch_exists(batch_no: str) -> AppError:
    return AppError(
        "BATCH_NO_REUSED",
        f"批次编号 {batch_no!r} 已存在，批次编号不得复用",
        409,
    )


def batch_not_found(batch_no: str) -> AppError:
    return AppError(
        "SOURCE_NOT_FOUND",
        f"批次 {batch_no!r} 不存在",
        404,
    )


def batch_quarantined(batch_no: str) -> AppError:
    return AppError(
        "BATCH_QUARANTINED",
        f"批次 {batch_no!r} 已隔离，隔离批次不得再投料",
        409,
    )


def batch_not_suspect(batch_no: str) -> AppError:
    return AppError(
        "BATCH_NOT_SUSPECT",
        f"批次 {batch_no!r} 当前不是疑似批次（编号不存在或已复核撤销），无可撤销的疑似标记",
        409,
        details={"batch_no": batch_no},
    )


def production_not_found(batch_no: str) -> AppError:
    return AppError(
        "PRODUCTION_NOT_FOUND",
        f"批次 {batch_no!r} 不是生产产出批次（无对应生产单），无可撤销的生产单",
        404,
        details={"batch_no": batch_no},
    )


def production_already_cancelled(batch_no: str) -> AppError:
    return AppError(
        "PRODUCTION_ALREADY_CANCELLED",
        f"产出批次 {batch_no!r} 的生产单已撤销，不得重复撤销",
        409,
        details={"batch_no": batch_no},
    )


def output_already_consumed(batch_no: str, consumer_nos: list[str]) -> AppError:
    return AppError(
        "OUTPUT_ALREADY_CONSUMED",
        f"产出批次 {batch_no!r} 已被后续生产投料（{', '.join(consumer_nos)}），"
        "仅允许撤销从未被后续生产投料的产出",
        409,
        details={"batch_no": batch_no, "consumed_by": sorted(consumer_nos)},
    )


def output_is_suspect(batch_no: str) -> AppError:
    return AppError(
        "OUTPUT_IS_SUSPECT",
        f"产出批次 {batch_no!r} 当前为疑似批次，须先经质检复核撤销疑似后方可撤销生产单",
        409,
        details={"batch_no": batch_no},
    )


def production_has_review(batch_no: str) -> AppError:
    return AppError(
        "PRODUCTION_HAS_REVIEW",
        f"产出批次 {batch_no!r} 存在质检复核记录，有复核记录的生产单不得撤销",
        409,
        details={"batch_no": batch_no},
    )


def batch_cancelled(batch_no: str) -> AppError:
    return AppError(
        "BATCH_CANCELLED",
        f"批次 {batch_no!r} 为已撤销生产单的产出，已撤销产出不得再投料",
        409,
        details={"batch_no": batch_no},
    )


def duplicate_split_target(batch_no: str) -> AppError:
    return AppError(
        "DUPLICATE_SPLIT_TARGET",
        f"同一笔分装中新编号 {batch_no!r} 重复出现，各子批次编号必须互异",
        422,
        details={"batch_no": batch_no},
    )


def output_already_split(batch_no: str, child_nos: list[str]) -> AppError:
    return AppError(
        "OUTPUT_ALREADY_SPLIT",
        f"产出批次 {batch_no!r} 已被分装为子批次（{', '.join(sorted(child_nos))}），"
        "父批次分装后视为曾被使用，不可再撤销其生产单",
        409,
        details={"batch_no": batch_no, "split_into": sorted(child_nos)},
    )


def insufficient(batch_no: str, requested: object, remaining: object,
                 location_code: str | None = None) -> AppError:
    if location_code is not None:
        message = (
            f"批次 {batch_no!r} 库位 {location_code!r} 余额不足："
            f"请求 {requested}，该库位剩余 {remaining}"
        )
    else:
        message = f"批次 {batch_no!r} 余量不足：请求 {requested}，剩余 {remaining}"
    return AppError(
        "INSUFFICIENT_BALANCE",
        message,
        409,
        details={
            "batch_no": batch_no,
            "requested": str(requested),
            "remaining": str(remaining),
            **({"location_code": location_code} if location_code is not None else {}),
        },
    )


def location_not_found(batch_no: str, location_code: str) -> AppError:
    return AppError(
        "LOCATION_NOT_FOUND",
        f"批次 {batch_no!r} 不存在库位 {location_code!r} 的余额记录",
        404,
        details={"batch_no": batch_no, "location_code": location_code},
    )


def transfer_same_location(location_code: str) -> AppError:
    return AppError(
        "TRANSFER_SAME_LOCATION",
        f"来源库位与目标库位均为 {location_code!r}，调拨要求两个不同的库位",
        422,
        details={"location_code": location_code},
    )


def transfer_no_exists(transfer_no: str) -> AppError:
    return AppError(
        "TRANSFER_NO_REUSED",
        f"调拨单号 {transfer_no!r} 已存在，调拨单号不得复用",
        409,
        details={"transfer_no": transfer_no},
    )


def duplicate_stock_check_line(batch_no: str, location_code: str) -> AppError:
    return AppError(
        "DUPLICATE_STOCK_CHECK_LINE",
        f"同一盘点单中批次 {batch_no!r} 库位 {location_code!r} 重复出现，"
        "各盘点行（批次 + 库位）必须互异",
        422,
        details={"batch_no": batch_no, "location_code": location_code},
    )


def stock_check_no_exists(stock_check_no: str) -> AppError:
    return AppError(
        "STOCK_CHECK_NO_REUSED",
        f"盘点单号 {stock_check_no!r} 已存在，盘点单号不得复用",
        409,
        details={"stock_check_no": stock_check_no},
    )


def stock_check_no_loss() -> AppError:
    return AppError(
        "STOCK_CHECK_NO_LOSS",
        "盘点单必须至少包含一条盘亏（实点量小于账面量）的行",
        422,
    )


def stock_check_book_changed(
    batch_no: str, location_code: str, book_qty: object, current_qty: object
) -> AppError:
    return AppError(
        "STOCK_CHECK_BOOK_CHANGED",
        f"批次 {batch_no!r} 库位 {location_code!r} 当前实存为 {current_qty}，"
        f"与申报账面量 {book_qty} 不一致，账面已变动，整单拒绝",
        409,
        details={
            "batch_no": batch_no,
            "location_code": location_code,
            "book_qty": str(book_qty),
            "current_qty": str(current_qty),
        },
    )


def stock_check_not_found(stock_check_no: str) -> AppError:
    return AppError(
        "STOCK_CHECK_NOT_FOUND",
        f"盘点单 {stock_check_no!r} 不存在",
        404,
        details={"stock_check_no": stock_check_no},
    )


def output_has_stock_loss(batch_no: str, stock_check_nos: list[str]) -> AppError:
    return AppError(
        "OUTPUT_HAS_STOCK_LOSS",
        f"产出批次 {batch_no!r} 已在盘点单（{', '.join(sorted(stock_check_nos))}）"
        "中发生盘亏，发生盘亏的生产产出不得撤销原生产单",
        409,
        details={"batch_no": batch_no, "stock_check_nos": sorted(stock_check_nos)},
    )


def unit_mismatch(batch_no: str, expected: str, actual: str) -> AppError:
    return AppError(
        "UNIT_MISMATCH",
        f"批次 {batch_no!r} 单位为 {expected!r}，与申报单位 {actual!r} 不一致",
        422,
    )
