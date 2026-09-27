"""MySQL JSON rendering may vary calibration cosines, never provenance."""
from copy import deepcopy
import json
import math
import sqlite3

import test_dismech_embeddings as fixtures
from reveal_backend import dismech_embeddings as embeddings
from reveal_backend.dismech_import import canonical, digest


class CalibrationStorageTests(fixtures.CaptureFixture):
    def setUp(self):
        super().setUp()
        self.complete()
        self.manifest, _ = embeddings.open_capture(self.output)
        with sqlite3.connect(self.output/'embeddings.sqlite3') as connection:
            self.calibration=json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])

    def check(self, report, config=None):
        row=(canonical(config or self.manifest['config']),self.manifest['dimensions'],
             self.manifest['expected_bindings'],self.manifest['expected_vectors'],0,0,canonical(report),'loading')
        embeddings._check_run(row,self.manifest,self.calibration)

    def test_mysql_one_or_two_ulp_cosine_rendering_preserves_valid_exact_provenance(self):
        for steps in (1,2):
            report=deepcopy(self.calibration)
            calibrations=[report['before'],report['after']]
            calibrations.extend(report['generation']['sessions'][0][key] for key in ('before_calibration','after_calibration'))
            for calibration in calibrations:
                value=calibration['probes'][0]['cosine_similarity']
                for _ in range(steps): value=math.nextafter(value,0)
                calibration['probes'][0]['cosine_similarity']=value
            self.check(report)

    def test_larger_or_invalid_cosine_change_is_rejected(self):
        for value in (0.9,math.nextafter(math.nextafter(math.nextafter(1.,0),0),0)):
            report=deepcopy(self.calibration)
            report['after']['probes'][0]['cosine_similarity']=value
            with self.assertRaises(ValueError): self.check(report)

    def test_source_config_probe_identity_metadata_and_counts_stay_exact(self):
        for case in ('source','probe','metadata','count'):
            report=deepcopy(self.calibration);config=deepcopy(self.manifest['config'])
            if case=='source': config['dismech_import_id']='0'*64
            elif case=='probe': report['before']['probes'][0]['stored_vector_sha256']='0'*64
            elif case=='count': report['generation']['vector_count']+=1
            else:
                session=report['generation']['sessions'][0]
                session['metadata']['backend']='invented-generator'
                session['metadata_sha256']=digest(canonical(session['metadata']))
            with self.subTest(case=case),self.assertRaises(ValueError): self.check(report,config)
