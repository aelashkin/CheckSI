"""Compare the fitter on identical reference inputs without replacing refusals."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np
from scipy.special import ndtri

spec=importlib.util.spec_from_file_location('dp5q_fit_analysis',Path(__file__).parents[1]/'scripts/dp5q-reference/analyze_fits.py')
analysis=importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class FitAnalysisTests(unittest.TestCase):
    def test_same_reference_input_refit_agrees_with_known_normal(self):
        levels=np.arange(1,100)/100
        quantiles=(12.+2.*ndtri(levels))[None,:]
        result=analysis.refit_reference(quantiles,np.array([12.]),np.array([2.]),np.array([12.]),levels)
        self.assertEqual(result['status'],'compared')
        self.assertAlmostEqual(result['mu_max_error_ppm'],0.,places=9)
        self.assertAlmostEqual(result['sigma_max_error_ppm'],0.,places=9)
        self.assertAlmostEqual(result['refitted_score_same_reference_assignment'],1.,places=9)
        self.assertFalse(any(result['N2_threshold_changes'].values()))

    def test_unidentifiable_reference_quantiles_are_an_explicit_fit_failure(self):
        levels=np.arange(1,100)/100
        result=analysis.refit_reference(np.ones((1,99)),np.array([1.]),np.array([1.]),np.array([1.]),levels)
        self.assertEqual(result['status'],'owned_refit_refused')
        self.assertEqual(result['groups'][0]['reason'],'unidentifiable_distribution')
        self.assertNotIn('refitted_score_same_reference_assignment',result)

    def test_group_shape_mismatch_cannot_silently_compare_atom_and_group_fits(self):
        with self.assertRaises(ValueError):
            analysis.refit_reference(np.zeros((2,99)),np.ones(1),np.ones(1),np.ones(1),np.arange(1,100)/100)

    def test_same_input_assignment_accepts_interchangeable_identical_values(self):
        quantiles=np.stack([np.arange(99),np.arange(99)+1.])
        result=analysis.assignment_controls([1.,2.],quantiles,[3.,3.],[3.,3.],[1,0])
        self.assertEqual(result['N5_reference_parsed_input']['status'],'assigned')
        self.assertTrue(result['N5_reference_parsed_input']['same_observed_values_as_reference'])
        self.assertAlmostEqual(result['N5_reference_parsed_input']['objective_error_ppm'],0.)
        self.assertAlmostEqual(result['N5_reference_parsed_input']['objective_ppm'],3.)

    def test_four_decimal_rounding_preserves_scoring_with_squared_assignment(self):
        mean=[1.00001,1.00002]
        quantiles=np.stack([np.arange(99),2.*np.arange(99)])
        result=analysis.assignment_controls(mean,quantiles,[.99994,1.000049],[.9999,1.],[0,1])
        self.assertIsNone(result['N5_reference_parsed_input']['reason'])
        self.assertEqual(result['N8_observation_precision']['original']['status'],'assigned')
        self.assertEqual(result['N8_observation_precision']['rounded']['status'],'assigned')
        self.assertFalse(result['N8_observation_precision']['status_or_reason_changed'])

    def test_changed_reference_assignment_is_reported_without_recalibrating_reference(self):
        quantiles=np.stack([np.arange(99),np.arange(99)+1.])
        result=analysis.assignment_controls([0.,10.],quantiles,[0.,10.],[0.,10.],[1,0])
        reference=result['N5_reference_parsed_input']
        self.assertFalse(reference['same_observed_values_as_reference'])
        self.assertEqual(reference['objective_ppm'],0.)
        self.assertEqual(reference['reference_objective_ppm'],20.)
        self.assertEqual(reference['objective_error_ppm'],20.)


if __name__=='__main__':
    unittest.main()
