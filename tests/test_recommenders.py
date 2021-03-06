import sys
import pandas as pd
import numpy as np
from copy import deepcopy

import time

from ml_recsys_tools.data_handlers.interaction_handlers_base import ObservationsDF
from ml_recsys_tools.datasets.prep_movielense_data import get_and_prep_data

from ml_recsys_tools.utils.logger import simple_logger as logger
from ml_recsys_tools.utils.instrumentation import pickle_size_mb

from ml_recsys_tools.utils.testing import TestCaseWithState
from tests.test_movielens_data import movielens_dir

rating_csv_path, users_csv_path, movies_csv_path = get_and_prep_data(movielens_dir)


DEBUG_ON = getattr(sys, 'gettrace', None) is not None


class TestRecommendersBasic(TestCaseWithState):

    TESTING_USER_IDS = ['test_user_11', 'test_user_22', 'test_user_33', 'test_user_44']
    TESTING_ITEM_IDS = ['test_item_11', 'test_item_22', 'test_item_33', 'test_item_44']
    user_id_col = 'userid'
    item_id_col = 'itemid'

    @classmethod
    def setUpClass(cls):
        cls.k = 10
        cls.n = 10
        cls.metric = 'n-MRR@%d' % cls.k

    def _setup_obs_handler(self):
        ratings_df = pd.read_csv(rating_csv_path)
        obs = ObservationsDF(ratings_df, uid_col=self.user_id_col, iid_col=self.item_id_col)
        obs = obs.sample_observations(n_users=1000, n_items=1000)
        self.state.train_obs, self.state.test_obs = obs.split_train_test(ratio=0.2, users_ratio=1.0)
        # add some fake data for sanity tests
        self.state.train_obs.df_obs = self._add_testing_obs_data(self.state.train_obs.df_obs)

    def _add_testing_obs_data(self, obs_df):
        last_row = obs_df.iloc[-1, :].copy()
        testing_df = pd.DataFrame(
            {self.user_id_col: sorted(self.TESTING_USER_IDS * len(self.TESTING_ITEM_IDS)),
             self.item_id_col: self.TESTING_ITEM_IDS * len(self.TESTING_USER_IDS)})

        # add other columns
        for col in obs_df.columns.difference(testing_df.columns):
            testing_df = testing_df.assign(**{col: last_row[col]})

        # exclude matching users and items from training (to later check that they are in predictions)
        testing_df = testing_df[~(testing_df[self.user_id_col].str[-2:] ==
                                  testing_df[self.item_id_col].str[-2:])]

        return obs_df.append(testing_df, sort=False)

    def test_b_1_lfm_recommender(self):
        self._setup_obs_handler()

        from ml_recsys_tools.recommenders.lightfm_recommender import LightFMRecommender
        lfm_rec = LightFMRecommender()
        lfm_rec.fit(self.state.train_obs, epochs=20)
        self.assertEqual(lfm_rec.fit_params['epochs'], 20)
        self._test_recommender(lfm_rec)
        # self._test_predict_for_user(lfm_rec)
        self.state.lfm_rec = lfm_rec

    def _test_get_recommendations(self, rec):
        # check format
        users = rec.all_users
        recs = rec.get_recommendations(n_rec=self.n)
        self.assertEqual(len(recs), len(users))
        self.assertListEqual(list(recs.columns), ['userid', 'itemid', 'prediction'])
        self.assertTrue(all(recs['itemid'].apply(len).values == self.n))

        # check predictions sorted
        for i in np.random.choice(np.arange(len(recs)), min(100, len(recs))):
            self.assertListEqual(recs['prediction'][i], sorted(recs['prediction'][i], reverse=True))

    def _test_get_similar_items(self, rec):
        # check format
        items = rec.all_items
        simils = rec.get_similar_items(n_simil=self.n)
        self.assertEqual(len(simils), len(items))
        self.assertListEqual(list(simils.columns), ['itemid_source', 'itemid', 'prediction'])
        self.assertTrue(all(simils['itemid'].apply(len).values == self.n))

        # sample check no self-similarities returned
        for i in np.random.choice(np.arange(len(simils)), min(100, len(simils))):
            self.assertTrue(simils['itemid_source'][i] not in simils['itemid'][i])

        # sample check predictions are sorted
        for i in np.random.choice(np.arange(len(simils)), min(100, len(simils))):
            self.assertListEqual(list(simils['prediction'][i]), sorted(simils['prediction'][i], reverse=True))

    def _test_predictions_on_fake_data(self, rec):
        # check that missing "interactions" are recommended
        for user in self.TESTING_USER_IDS:
            recos = rec.get_recommendations(user_ids=[user], n_rec=10).iloc[0][rec._item_col]
            logger.info(f'{user} {recos}')
            self.assertTrue(user.replace('user', 'item') in recos)

        # check that test items are similar to each other
        for item in self.TESTING_ITEM_IDS:
            simils = rec.get_similar_items(item_ids=[item], n_simil=10).iloc[0][rec._item_col]
            logger.info(f'{item} {simils}')
            self.assertTrue(len(set(simils).intersection(set(self.TESTING_ITEM_IDS))) >= 3)

    def _test_recommender(self, rec):
        self._test_get_recommendations(rec)
        self._test_get_similar_items(rec)
        self._test_predict_for_user(rec)
        self._test_predictions_on_fake_data(rec)
        self._test_custom_exclusions(rec)

    def _test_predict_for_user(self, rec):
        user = rec.all_users[0]
        items = rec.all_items[:50]

        ts = time.time()
        preds_1 = rec.predict_for_user(user_id=user, item_ids=items)
        elapsed = time.time() - ts
        scores = preds_1[rec._prediction_col].tolist()

        # test format
        # columns
        self.assertListEqual(preds_1.columns.tolist(),
                             [rec._user_col, rec._item_col, rec._prediction_col])
        # length
        self.assertEqual(len(preds_1), len(items))

        # test sorted descending
        self.assertTrue(scores[::-1] == sorted(scores))

        # test combine with original order makes first item in original order higher in results
        preds_2 = rec.predict_for_user(user_id=user, item_ids=items, combine_original_order=True)
        ind_item = lambda item, preds: np.argmax(preds[rec._item_col].values == item)
        ind_diffs = np.array([ind_item(item, preds_1) - ind_item(item, preds_2)
                              for item in items])
        self.assertEqual(ind_diffs.sum(), 0)
        self.assertGreater(ind_diffs[:(len(ind_diffs) // 2)].sum(), 0)  # first items rank higher

        # test training items predictions are last
        train_item = rec.item_ids([rec.train_mat[rec.user_inds([user])[0],:].indices[0]])
        preds_3 = rec.predict_for_user(user_id=user, item_ids=np.concatenate([items, train_item]))
        train_preds = preds_3[preds_3[rec._item_col] == train_item[0]][rec._prediction_col]
        self.assertTrue(all(train_preds == preds_3[rec._prediction_col].min()))

        # test unknown items are last
        new_items = 'new_item'
        preds_4 = rec.predict_for_user(user_id=user, item_ids=np.concatenate([items, [new_items]]))
        new_preds = preds_4[preds_4[rec._item_col] == new_items][rec._prediction_col]
        self.assertTrue(all(new_preds == preds_4[rec._prediction_col].min()))

        # test for unknown user all predictions are the same
        preds_5 = rec.predict_for_user(user_id='new_user', item_ids=items)
        self.assertEqual(preds_5[rec._prediction_col].min(), preds_5[rec._prediction_col].max())

        # test doesn't take more than 0.05 second
        logger.info(f'predict_for_user for {rec} took {elapsed:.3f} seconds.')
        self.assertGreater(0.06 * (1 + 2 * int(DEBUG_ON)), elapsed)  #  allow more tme if debugging

    def _test_custom_exclusions(self, rec):
        rec = deepcopy(rec)

        # baseline
        rep_reg = rec.eval_on_test_by_ranking(
            self.state.test_obs.df_obs, prefix='lfm regular ', n_rec=200, k=self.k)

        # take half of the test for exclusion
        exc_df = self.state.test_obs.df_obs.sample(len(self.state.test_obs.df_obs) // 2)
        exc_obs = self.state.test_obs.filter_interactions_by_df(exc_df, mode='keep')

        metric_ind = rep_reg.columns.tolist().index('n-MRR')

        # custom exclusion with training
        rec.set_exclude_mat(exc_obs)
        rep_exc_default = rec.eval_on_test_by_ranking(
            [self.state.train_obs, self.state.test_obs, exc_obs], prefix='lfm regular ', n_rec=200, k=self.k)
        # train performance is the same
        self.assertAlmostEqual(rep_exc_default.iloc[0, metric_ind], rep_reg.iloc[0, metric_ind], places=1)
        # train performance when train excluded is chance
        self.assertAlmostEqual(rep_exc_default.iloc[1, metric_ind], 0.0, places=1)
        # test performance with exclusion is lower than without
        self.assertLess(rep_exc_default.iloc[2, metric_ind], rep_reg.iloc[1, metric_ind] - 0.03)
        # test performance on exclusion only is chance
        self.assertAlmostEqual(rep_exc_default.iloc[3, metric_ind], 0.0, places=1)

        rec.set_exclude_mat(exc_obs, exclude_training=False)
        rep_exc_train = rec.eval_on_test_by_ranking(
            [self.state.train_obs, self.state.test_obs, exc_obs], prefix='lfm regular ', n_rec=200, k=self.k)
        # train performance is the same
        self.assertAlmostEqual(rep_exc_train.iloc[0, metric_ind], rep_reg.iloc[0, metric_ind], places=1)
        # train performance with exclusion is NOT chance (because train is not excluded)
        self.assertAlmostEqual(rep_exc_train.iloc[1, metric_ind], rep_reg.iloc[0, metric_ind], places=1)
        # test performance with exclusion is lower than without
        self.assertLess(rep_exc_train.iloc[2, metric_ind], rep_reg.iloc[1, metric_ind] - 0.03)
        # test performance on exclusion only is chance
        self.assertAlmostEqual(rep_exc_train.iloc[3, metric_ind], 0.0, places=1)

    def test_b_2_lfm_rec_evaluation(self):
        k = self.k

        rep_exact = self.state.lfm_rec.eval_on_test_by_ranking_exact(
            self.state.test_obs.df_obs, prefix='lfm regular exact ', k=k)
        logger.info(rep_exact)

        rep_reg = self.state.lfm_rec.eval_on_test_by_ranking(
            self.state.test_obs.df_obs, prefix='lfm regular ', n_rec=200, k=k)
        logger.info(rep_reg)

        self.assertListEqual(list(rep_reg.columns), list(rep_exact.columns))

        # test that those fields are almost equal for the two test methods
        logger.info('deviations from exact evaluation')
        for col in rep_reg.columns:
            deviations = abs(1 - (rep_exact[col].values / rep_reg[col].values))
            logger.info(f'{col}: {deviations}')
            if 'AUC' in col:
                self.assertTrue(all(deviations < 0.1))
            elif 'coverage' in col:
                self.assertTrue(all(deviations < 0.03))
            else:
                self.assertTrue(all(deviations < 0.01))


    def test_b_3_lfm_early_stop(self):
        lfm_rec = deepcopy(self.state.lfm_rec)
        lfm_rec.fit(self.state.train_obs, epochs=1)
        prev_epochs = lfm_rec.fit_params['epochs']

        lfm_rec.fit_with_early_stop(
            self.state.train_obs,
            epochs_max=5, epochs_step=5, stop_patience=1,
            valid_ratio=0.2, metric=self.metric, k=self.k,
            refit_on_all=False, plot_convergence=False)

        sut_epochs = lfm_rec.fit_params['epochs']

        # check that epochs parameter changed
        self.assertNotEqual(prev_epochs, sut_epochs)

        # check that in the report dataframe the maximum metric value is for our new epoch number
        self.assertEqual(lfm_rec.early_stop_metrics_df[self.metric].idxmax(), sut_epochs)

    def test_b_4_lfm_hp_search(self):
        lfm_rec = deepcopy(self.state.lfm_rec)
        space = lfm_rec.guess_search_space()
        n_iters = 4
        hp_space = dict(
            no_components=space.Integer(10, 40),
            epochs=space.Integer(5, 20),
            item_alpha=space.Real(1e-8, 1e-5, prior='log-uniform')
        )
        hp_results = lfm_rec.hyper_param_search(
            self.state.train_obs,
            metric=self.metric,
            k=self.k,
            plot_graph=False,
            hp_space=hp_space,
            n_iters=n_iters,
        )

        # check that best model works
        self._test_recommender(hp_results.best_model)

        # check that hp space and params have same keys
        best_params = hp_results.best_params
        self.assertListEqual(sorted(best_params.keys()), sorted(hp_space.keys()))

        # check report format
        rep = hp_results.report
        self.assertEqual(len(rep), n_iters)

        # check that best values in report are best values for best params
        best_params_sut = rep.loc[rep['target_loss'].idxmin()][best_params.keys()].to_dict()
        self.assertDictEqual(best_params_sut, best_params)

    def test_c_cooc_recommender(self):
        from ml_recsys_tools.recommenders.similarity_recommenders import ItemCoocRecommender

        item_cooc_rec = ItemCoocRecommender()
        item_cooc_rec.fit(self.state.train_obs)
        item_cooc_rep = item_cooc_rec.eval_on_test_by_ranking(self.state.test_obs, prefix='item cooccurrence ')
        logger.info(item_cooc_rep)
        self._test_recommender(item_cooc_rec)
        self.state.item_cooc_rec = item_cooc_rec

    def test_c_als_recommender(self):
        from ml_recsys_tools.recommenders.implib_recommenders import ALSRecommender

        als_rec = ALSRecommender()
        als_rec.fit(self.state.train_obs)
        als_rep = als_rec.eval_on_test_by_ranking(self.state.test_obs, prefix='als ')
        logger.info(als_rep)
        self._test_recommender(als_rec)

    def test_c_spotlight_implicit_recommender(self):
        from ml_recsys_tools.recommenders.spotlight_recommenders import EmbeddingFactorsRecommender

        rec = EmbeddingFactorsRecommender()
        # trying to balance flakiness and speed
        rec.set_params(embedding_dim=32, batch_size=1<<10,
                       num_negative_samples=10, n_iter=5)
        rec.fit(self.state.train_obs)
        report = rec.eval_on_test_by_ranking(self.state.test_obs, prefix='spot ')
        logger.info(report)
        self._test_recommender(rec)

    def test_d_comb_rank_ens(self):
        from ml_recsys_tools.recommenders.combination_ensembles import CombinedRankEnsemble

        comb_ranks_rec = CombinedRankEnsemble(
            recommenders=[self.state.lfm_rec, self.state.item_cooc_rec])
        comb_rank_rep = comb_ranks_rec.eval_on_test_by_ranking(self.state.test_obs, prefix='combined ranks ')
        logger.info(comb_rank_rep)
        self._test_recommender(comb_ranks_rec)

    def test_d_comb_simil_ens(self):
        from ml_recsys_tools.recommenders.combination_ensembles import CombinedSimilRecoEns

        comb_simil_rec = CombinedSimilRecoEns(
            recommenders=[self.state.lfm_rec, self.state.item_cooc_rec])
        comb_simil_rec.fit(self.state.train_obs)
        comb_simil_rep = comb_simil_rec.eval_on_test_by_ranking(self.state.test_obs, prefix='combined simils ')
        logger.info(comb_simil_rep)
        self._test_recommender(comb_simil_rec)

    def test_d_lfm_reduce_memory_size(self):
        lfm_rec = deepcopy(self.state.lfm_rec)
        mem_before = pickle_size_mb(lfm_rec)
        lfm_rec.reduce_memory_for_serving()
        mem_after = pickle_size_mb(lfm_rec)
        self._test_recommender(lfm_rec)
        self.assertGreater(mem_before, mem_after)

