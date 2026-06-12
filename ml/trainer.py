# ml/trainer.py
"""
Управление жизненным циклом ML-моделей.
staging -> production -> archive
"""
import os, json, shutil, glob
from datetime import datetime
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import ML_MODELS_DIR, ML_STAGING_DIR

PRODUCTION_DIR = os.path.join(os.path.dirname(ML_MODELS_DIR), "production")
ARCHIVE_DIR    = os.path.join(os.path.dirname(ML_MODELS_DIR), "archive")

for _d in [ML_STAGING_DIR, PRODUCTION_DIR, ARCHIVE_DIR]:
    os.makedirs(_d, exist_ok=True)


class ModelTrainer:
    """Управляет перемещением моделей между staging / production / archive."""

    def promote(self, pair: str) -> bool:
        """Перемещает модель из staging в production."""
        src_model = os.path.join(ML_STAGING_DIR, f"{pair}_model.pkl")
        src_meta  = os.path.join(ML_STAGING_DIR, f"{pair}_meta.json")
        if not os.path.exists(src_model):
            print(f"[trainer] staging model not found: {src_model}")
            return False
        self._archive_current(pair)
        dst_model = os.path.join(PRODUCTION_DIR, f"{pair}_model.pkl")
        dst_meta  = os.path.join(PRODUCTION_DIR, f"{pair}_meta.json")
        shutil.move(src_model, dst_model)
        if os.path.exists(src_meta):
            shutil.move(src_meta, dst_meta)
        print(f"[trainer] {pair} promoted to production")
        return True

    def _archive_current(self, pair: str):
        """Архивирует текущую production-модель."""
        prod_model = os.path.join(PRODUCTION_DIR, f"{pair}_model.pkl")
        if not os.path.exists(prod_model):
            return
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(ARCHIVE_DIR, f"{pair}_model_{ts}.pkl")
        shutil.move(prod_model, dst)
        prod_meta = os.path.join(PRODUCTION_DIR, f"{pair}_meta.json")
        if os.path.exists(prod_meta):
            shutil.move(prod_meta,
                os.path.join(ARCHIVE_DIR, f"{pair}_meta_{ts}.json"))
        print(f"[trainer] {pair} archived as {ts}")

    def status(self) -> dict:
        """Возвращает статус моделей по всем парам."""
        result = {}
        for pkl in glob.glob(os.path.join(PRODUCTION_DIR, "*_model.pkl")):
            pair = os.path.basename(pkl).replace("_model.pkl", "")
            meta_path = os.path.join(PRODUCTION_DIR, f"{pair}_meta.json")
            meta = {}
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
            result[pair] = {"production": True, "meta": meta}
        for pkl in glob.glob(os.path.join(ML_STAGING_DIR, "*_model.pkl")):
            pair = os.path.basename(pkl).replace("_model.pkl", "")
            result.setdefault(pair, {})["staging"] = True
        return result

    def promote_all(self):
        """Продвигает все модели из staging в production."""
        for pkl in glob.glob(os.path.join(ML_STAGING_DIR, "*_model.pkl")):
            pair = os.path.basename(pkl).replace("_model.pkl", "")
            self.promote(pair)
