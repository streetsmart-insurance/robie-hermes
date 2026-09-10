import sys, asyncio, logging
from pathlib import Path

sys.path.insert(0, '/opt/renewal-automation-system')
from src.ezlynx.document_uploader import EZLynxDocumentUploader

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('batch_uploader')

base_dir = Path('/opt/busy-borg/data/policy_pdfs')

queue = [
    ('199797187', 'Sunil Dixit', '4236565', '4236565_Mortgagee_Confirmation_3866489.pdf'),
    ('21586719', 'Blanca Tipan', 'HONJ026092', 'HONJ026092_Mortgagee_Confirmation_3866467.pdf'),
    ('49338990', 'Jeffrey Abdin', '4204205', '4204205_Mortgagee_Confirmation_3866490.pdf'),
    ('83644603', 'Laura Oloughlin', '4701-2000-4650', '4701-2000-4650_Mortgagee_Confirmation_MCI_Chase_4023039902.pdf'),
    ('65751209', 'George Fahmy', '6053683956331', '6053683956331_Mortgagee_Confirmation_IHIVE_3866240.pdf'),
    ('66950708', 'JAIMIE WENDT', 'H  2371320', 'H_2371320_Mortgagee_Confirmation_EXPRESS_SUCCESSFUL_SAVE.pdf'),
    ('54893172', 'Hua Li', 'DPNJ2024100008-25', 'DPNJ2024100008-25_Mortgagee_Confirmation_3866467.pdf'),
    ('59444537', 'Tammy Soluri', 'GB-2025-22765', 'GB-2025-22765_Mortgagee_Confirmation_3866488.pdf'),
    ('190087062', 'Gary Dean', 'GB-2025-25402', 'GB-2025-25402_Mortgagee_Confirmation_3866499.pdf'),
    ('22234682', 'Vasiliy Shafar', 'HONJ2015070057-26', 'HONJ2015070057-26_Mortgagee_Confirmation_3866500.pdf'),
    ('21586802', 'Brian Perskin', 'GB-2024-22986', 'GB-2024-22986_Mortgagee_Confirmation_EXPRESS_SUCCESSFUL_SAVE.pdf')
]

async def run_batch():
    uploader = EZLynxDocumentUploader()
    for aid, name, pol, fname in queue:
        pdf_path = base_dir / aid / fname
        if not pdf_path.exists():
            logger.error(f'File missing for {name}: {pdf_path}')
            continue
        logger.info(f'Uploading confirmation for {name} ({aid})...')
        doc_title = Path(fname).stem
        res = await uploader.upload_document(
            applicant_id=aid,
            file_path=pdf_path,
            policy_number=pol,
            doc_type='renewal',
            doc_title=doc_title,
            target_folder='Renewal Offers/Declarations'
        )
        logger.info(f'Result for {name}: {res.get("success")} - {res.get("error", "OK")}')
        await asyncio.sleep(2)

if __name__ == '__main__':
    asyncio.run(run_batch())
