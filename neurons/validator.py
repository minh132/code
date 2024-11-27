# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2024 Broke

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
import dotenv

dotenv.load_dotenv()

import sys
import time
import random
import asyncio
import threading

import bittensor as bt
from typing import Awaitable, Tuple
from code_bert_score import BERTScorer
from langchain_openai import ChatOpenAI

from coding.validator import forward, forward_organic_synapse
from coding.rewards.pipeline import RewardPipeline
from coding.protocol import StreamCodeSynapse
from coding.datasets import DatasetManager
from coding.repl import REPLClient

# import base validator class which takes care of most of the boilerplate
from coding.utils.config import config as util_config
from coding.base.validator import BaseValidatorNeuron

class Validator(BaseValidatorNeuron):
    """
    Your validator neuron class. You should use this class to define your validator's behavior. In particular, you should replace the forward function with your own logic.

    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a validator such as keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    def __init__(self, config=None):
        if not config:
            config = util_config(self)
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()

        self.llm = ChatOpenAI(
            base_url=self.config.neuron.model_url,
            model_name=self.config.neuron.model_id,
            api_key=self.config.neuron.vllm_api_key,
        ) 
        self.repl = REPLClient()
        self.code_scorer = BERTScorer(lang="python")
        self.dataset_manager = DatasetManager(self.config)
        self.active_tasks = [
            task
            for task, p in zip(
                self.config.neuron.tasks, self.config.neuron.task_weights
            )
            if p > 0
        ]
        # Load the reward pipeline
        self.reward_pipeline = RewardPipeline(
            selected_tasks=self.active_tasks,
            device=self.device,
            code_scorer=self.code_scorer,
        )

    def _forward(
        self, synapse: StreamCodeSynapse
    ) -> (
        StreamCodeSynapse
    ):  # TODO remove this since its duplicate code, could be handled better
        """
        forward method that is called when the validator is queried with an axon
        """
        response = forward_organic_synapse(self, synapse=synapse)

        def _run():
            asyncio.run(forward(self, synapse))

        if random.random() < self.config.neuron.percent_organic_score:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(forward(self, synapse))
            except RuntimeError:  # No event loop running
                threading.Thread(target=_run).start()
            # return the response
        return response

    async def forward(self, synapse: StreamCodeSynapse) -> Awaitable:
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        return forward(self, synapse)

    # TODO make it so that the only thing accepted is the subnet owners hotkey + the validators coldkey
    async def blacklist(self, synapse: StreamCodeSynapse) -> Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted and thus ignored. Your implementation should
        define the logic for blacklisting requests based on your needs and desired security parameters.

        Blacklist runs before the synapse data has been deserialized (i.e. before synapse.data is available).
        The synapse is instead contructed via the headers of the request. It is important to blacklist
        requests before they are deserialized to avoid wasting resources on requests that will be ignored.

        Args:
            synapse (template.protocol.Dummy): A synapse object constructed from the headers of the incoming request.

        Returns:
            Tuple[bool, str]: A tuple containing a boolean indicating whether the synapse's hotkey is blacklisted,
                            and a string providing the reason for the decision.

        This function is a security measure to prevent resource wastage on undesired requests. It should be enhanced
        to include checks against the metagraph for entity registration, validator status, and sufficient stake
        before deserialization of synapse data to minimize processing overhead.

        Example blacklist logic:
        - Reject if the hotkey is not a registered entity within the metagraph.
        - Consider blacklisting entities that are not validators or have insufficient stake.

        In practice it would be wise to blacklist requests from entities that are not validators, or do not have
        enough stake. This can be checked via metagraph.S and metagraph.validator_permit. You can always attain
        the uid of the sender via a metagraph.hotkeys.index( synapse.dendrite.hotkey ) call.

        Otherwise, allow the request to be processed further.
        """
        if synapse.dendrite.hotkey == "5Fy7c6skhxBifdPPEs3TyytxFc7Rq6UdLqysNPZ5AMAUbRQx":
            return False, "Subnet owner hotkey"
        return True, "Blacklisted"

    async def priority(self, synapse: StreamCodeSynapse) -> float:
        """
        The priority function determines the order in which requests are handled. More valuable or higher-priority
        requests are processed before others. You should design your own priority mechanism with care.

        This implementation assigns priority to incoming requests based on the calling entity's stake in the metagraph.

        Args:
            synapse (template.protocol.Dummy): The synapse object that contains metadata about the incoming request.

        Returns:
            float: A priority score derived from the stake of the calling entity.

        Miners may recieve messages from multiple entities at once. This function determines which request should be
        processed first. Higher values indicate that the request should be processed first. Lower values indicate
        that the request should be processed later.

        Example priority logic:
        - A higher stake results in a higher priority value.
        """
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return 0.0

        # TODO(developer): Define how miners should prioritize requests.
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        priority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: {priority}"
        )
        return priority


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    with Validator() as validator:
        while True:
            if not validator.thread.is_alive():
                bt.logging.error("Child thread has exited, terminating parent thread.")
                sys.exit(1)  # Exit the parent thread if the child thread dies
            bt.logging.info(f"Validator running... {time.time()}")
            time.sleep(5)
