from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DQObject, DQElement
from ..schemas import ObjectOut, ElementOut

router = APIRouter(prefix="/api/objects", tags=["objects"])


@router.get("", response_model=list[ObjectOut])
def list_objects(db: Session = Depends(get_db)):
    return db.query(DQObject).filter(DQObject.active_flag == True).all()  # noqa: E712


@router.get("/{object_id}/elements", response_model=list[ElementOut])
def list_elements(object_id: int, db: Session = Depends(get_db)):
    return (
        db.query(DQElement)
        .filter(DQElement.object_id == object_id, DQElement.active_flag == True)  # noqa: E712
        .all()
    )
