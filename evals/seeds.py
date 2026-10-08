"""Historiales sembrados para escenarios que necesitan un estado previo determinista."""

from app.db import FileVersion, Message, Review, Run

COUPON_TICKET = ("Agregar validación de cupones de descuento al carrito de compras: el cupón debe existir, "
                 "no estar expirado y no exceder el monto del carrito.")
EXHAUSTED_RESULT = "Se alcanzó el límite de intentos de revisión. Error persistente detectado."

# Código con dos defectos deliberados: la expiración está invertida y no se compara con el carrito.
BUGGY_COUPONS = '''from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Coupon:
    code: str
    discount: float
    expires_on: date


COUPONS = {
    "BIENVENIDA10": Coupon("BIENVENIDA10", 10.0, date(2030, 1, 31)),
    "VERANO50": Coupon("VERANO50", 50.0, date(2024, 8, 31)),
}


class CouponError(ValueError):
    pass


def validate_coupon(code: str, cart_total: float, today: date) -> float:
    """Devuelve el descuento aplicable o lanza CouponError."""
    coupon = COUPONS.get(code.strip().upper())
    if coupon is None:
        raise CouponError("El cupón no existe.")
    if coupon.expires_on > today:
        raise CouponError("El cupón está expirado.")
    return coupon.discount
'''

REVIEW_FEEDBACK = [
    "La condición de expiración está invertida: rechaza cupones vigentes y acepta los expirados.",
    "No se valida que el descuento no exceda el total del carrito, como pide el ticket.",
]


def exhausted_coupon(store, conversation_id: str) -> None:
    """Un ticket de cupones que agotó los intentos, con el último código y el feedback del revisor."""
    with store() as db:
        message = Message(conversation_id=conversation_id, role="user", content=COUPON_TICKET)
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation_id, message_id=message.id, status="exhausted", attempts=3,
                  result=EXHAUSTED_RESULT, validation_status="not_executed")
        db.add(run)
        db.flush()
        db.add(FileVersion(conversation_id=conversation_id, run_id=run.id, attempt=3,
                           path="coupons.py", content=BUGGY_COUPONS))
        db.add(Review(run_id=run.id, attempt=3, content={
            "approved": False, "summary": "Persisten dos defectos en la validación.", "feedback": REVIEW_FEEDBACK}))
        db.add(Message(conversation_id=conversation_id, role="assistant", content=EXHAUSTED_RESULT))
        db.commit()


SEEDS = {"exhausted_coupon": exhausted_coupon}
