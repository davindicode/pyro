# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import funsor

from pyro.distributions.util import copy_docs_from
from pyro.infer import ELBO
from pyro.infer import TraceEnum_ELBO as OrigTraceEnum_ELBO
from pyro.poutine.util import prune_subsample_sites

from pyro.contrib.funsor import to_data, to_funsor
from pyro.contrib.funsor.handlers import enum, replay, trace

funsor.set_backend("torch")


def expectation(log_probs, costs, sum_vars, prod_vars):
    result = to_funsor(0, output=funsor.reals())
    for cost in costs:
        log_prob = funsor.sum_product.sum_product(
            funsor.ops.logaddexp, funsor.ops.add, log_probs,
            plates=prod_vars, eliminate=(prod_vars | sum_vars) - frozenset(cost.inputs)
        )
        term = funsor.Integrate(log_prob, cost, sum_vars & frozenset(cost.inputs))
        term = term.reduce(funsor.ops.add, prod_vars & frozenset(cost.inputs))
        result += term
    return result


@copy_docs_from(OrigTraceEnum_ELBO)
class TraceEnum_ELBO(ELBO):

    def _get_trace(self, *args, **kwargs):
        raise ValueError("shouldn't be here")

    def differentiable_loss(self, model, guide, *args, **kwargs):
        with enum(first_available_dim=-self.max_plate_nesting-1):
            guide_tr = trace(guide).get_trace(*args, **kwargs)
            model_tr = trace(replay(model, trace=guide_tr)).get_trace(*args, **kwargs)

        terms = {
            "model": {"log_factors": [], "log_measures": [], "plate_vars": frozenset(), "measure_vars": frozenset()},
            "guide": {"log_factors": [], "log_measures": [], "plate_vars": frozenset(), "measure_vars": frozenset()},
        }
        for role, tr in zip(("model", "guide"), map(prune_subsample_sites, (model_tr, guide_tr))):
            for name, node in tr.nodes.items():
                if node["type"] != "sample":
                    continue
                # if a site is enumerated in the model, measure but no log_prob
                if name in guide_tr.nodes or node['is_observed']:
                    terms[role]["log_factors"].append(
                        node["funsor"]["log_prob"] if role == "model" else -node["funsor"]["log_prob"])
                if node["funsor"].get("log_measure", None) is not None:
                    terms[role]["log_measures"].append(node["funsor"]["log_measure"])
                    terms[role]["measure_vars"] |= frozenset(node["funsor"]["log_measure"].inputs)
                terms[role]["plate_vars"] |= frozenset(f.name for f in node["cond_indep_stack"] if f.vectorized)
                terms[role]["measure_vars"] |= frozenset(node["funsor"]["log_prob"].inputs)

        with funsor.interpreter.interpretation(funsor.terms.lazy):
            # contract out auxiliary variables in the model
            model_aux_vars = terms["model"]["measure_vars"] - terms["model"]["plate_vars"] - \
                (terms["guide"]["measure_vars"] | terms["guide"]["plate_vars"])
            if model_aux_vars:
                contracted_terms = [t for t in terms["model"]["log_factors"] if model_aux_vars.intersection(t.inputs)]
                uncontracted_terms = [t for t in terms["model"]["log_factors"]
                                      if not model_aux_vars.intersection(t.inputs)]
                terms["model"]["log_factors"] = uncontracted_terms + funsor.sum_product.partial_sum_product(
                    funsor.ops.logaddexp, funsor.ops.add, terms["model"]["log_measures"] + contracted_terms,
                    plates=terms["model"]["plate_vars"], eliminate=model_aux_vars
                )

            # compute remaining plates and sum_dims
            plate_vars = (terms["model"]["plate_vars"] | terms["guide"]["plate_vars"]) - model_aux_vars
            sum_vars = (terms["model"]["measure_vars"] | terms["guide"]["measure_vars"]) - model_aux_vars - plate_vars

            # integrate out guide variables
            elbo = expectation(terms["guide"]["log_measures"],
                               terms["model"]["log_factors"] + terms["guide"]["log_factors"],
                               sum_vars, plate_vars)

        with funsor.memoize.memoize():
            return -to_data(funsor.optimizer.apply_optimizer(elbo))
