"""
create_tables.py
-----------------
Creates the 7-table V1 schema. Also registers the Account object and its 16
CDEs in the catalog (DQ_OBJECT / DQ_ELEMENT) -- that's catalog setup, not
rule data. NO rows are ever inserted into DQ_RULE here; rules are created
and approved by users through the UI only.

Usage:
    python create_tables.py            # create tables + seed catalog if empty
    python create_tables.py --reset    # drop everything and recreate
"""

import sys
from app.database import engine
from app.models import Base, DQObject, DQElement, CDE_COLUMNS
from sqlalchemy.orm import Session

DATA_TYPE_HINTS = {
    "Name": "string", "Type": "string", "Industry": "string", "Phone": "string",
    "Website": "string", "Region__c": "string",
    "BillingCity": "string", "BillingCountry": "string", "BillingPostalCode": "string",
    "BillingState": "string", "BillingStreet": "string",
    "ShippingCity": "string", "ShippingCountry": "string", "ShippingPostalCode": "string",
    "ShippingState": "string", "ShippingStreet": "string",
}


def main():
    if "--reset" in sys.argv:
        print("Dropping all tables...")
        Base.metadata.drop_all(engine)

    print("Creating tables...")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        existing = db.query(DQObject).filter_by(object_name="Account").first()
        if existing:
            print("Catalog already seeded (Account object exists) -- skipping.")
            return

        account = DQObject(
            object_name="Account",
            source_system="SFDC",
            source_object_name="Account",
            record_key_column="Id",
            active_flag=True,
        )
        db.add(account)
        db.flush()  # get account.object_id

        for name in CDE_COLUMNS:
            db.add(DQElement(
                object_id=account.object_id,
                element_name=name,
                source_column_name=name,
                data_type=DATA_TYPE_HINTS.get(name, "string"),
                active_flag=True,
            ))
        db.commit()
        print(f"Seeded catalog: 1 object (Account), {len(CDE_COLUMNS)} elements.")
        print("No DQ_RULE rows created -- add rules through the UI/API.")


if __name__ == "__main__":
    main()
