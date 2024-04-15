# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A Langchain LLM component for connecting to Triton + TensorRT LLM backend."""
import json
import queue
import time
from functools import partial
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from langchain.callbacks.manager import CallbackManagerForLLMRun
from langchain.llms.base import LLM
from langchain.pydantic_v1 import Field, root_validator

if TYPE_CHECKING:
    import tritonclient.grpc as grpcclient


class TensorRTLLM(LLM):
    """A custom Langchain LLM class that integrates with TRTLLM triton models.

    Arguments:
    server_url: (str) The URL of the Triton inference server to use.
    model_name: (str) The name of the Triton TRT model to use.
    temperature: (str) Temperature to use for sampling
    top_p: (float) The top-p value to use for sampling
    top_k: (float) The top k values use for sampling
    beam_width: (int) Last n number of tokens to penalize
    repetition_penalty: (int) Last n number of tokens to penalize
    length_penalty: (float) The penalty to apply repeated tokens
    tokens: (int) The maximum number of tokens to generate.
    client: The client object used to communicate with the inference server
    """

    server_url: str = Field(None, alias="server_url")

    # # all the optional arguments
    model_name: str = "ensemble"
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 0
    top_k: Optional[int] = 1
    tokens: Optional[int] = 100
    beam_width: Optional[int] = 1
    repetition_penalty: Optional[float] = 1.0
    length_penalty: Optional[float] = 1.0
    client: Any
    streaming: Optional[bool] = True

    @root_validator()
    @classmethod
    def validate_environment(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate that python package exists in environment."""
        try:
            values["client"] = TritonClient(values["server_url"])

        except ImportError as err:
            raise ImportError(
                "Could not import triton client python package. "
                "Please install it with `pip install tritonclient[all]`."
            ) from err
        return values

    @property
    def _get_model_default_parameters(self) -> Dict[str, Any]:
        return {
            "tokens": self.tokens,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "repetition_penalty": self.repetition_penalty,
            "length_penalty": self.length_penalty,
            "beam_width": self.beam_width,
        }

    @property
    def _invocation_params(self, **kwargs: Any) -> Dict[str, Any]:
        params = {**self._get_model_default_parameters, **kwargs}
        return params

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get all the identifying parameters."""
        return {
            "server_url": self.server_url,
            "model_name": self.model_name,
        }

    @property
    def _llm_type(self) -> str:
        return "triton_tensorrt"

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """
        Execute an inference request.

        Args:
            prompt: The prompt to pass into the model.
            stop: A list of strings to stop generation when encountered

        Returns:
            The string generated by the model
        """
        # pylint: disable-next=import-outside-toplevel
        from tritonclient.utils import InferenceServerException

        text_callback = None
        if run_manager:
            text_callback = partial(run_manager.on_llm_new_token, verbose=self.verbose)

        invocation_params = self._get_model_default_parameters
        invocation_params.update(kwargs)
        invocation_params["prompt"] = [[prompt]]
        model_params = self._identifying_params
        model_params.update(kwargs)

        result_queue: queue.Queue[Dict[str, str]] = queue.Queue()
        self.client.load_model(model_params["model_name"])
        self.client.request_streaming(
            model_params["model_name"], result_queue, **invocation_params
        )

        response = ""
        send_tokens = True
        while True:
            response_streaming = result_queue.get()

            if response_streaming is None or isinstance(
                response_streaming, InferenceServerException
            ):
                self.client.close_streaming()
                break
            token = response_streaming["OUTPUT_0"]
            if token in STOP_WORDS:
                send_tokens = False
            if text_callback and send_tokens:
                if response_streaming["OUTPUT_0"] == "<0x0A>":
                    token = "\n"  # nosec
                text_callback(token)
                response = response + token
        return response


STOP_WORDS = ["</s>"]
BAD_WORDS = [""]
RANDOM_SEED = 0


class TritonClient:
    """An abstraction of the connection to a triton inference server."""

    def __init__(self, server_url: str) -> None:
        """Initialize the client."""
        # pylint: disable-next=import-outside-toplevel
        import tritonclient.grpc as grpcclient

        self.server_url = server_url
        self.client = grpcclient.InferenceServerClient(server_url)

    def load_model(self, model_name: str, timeout: int = 1000) -> None:
        """Load a model into the server."""
        # Expect running triton with --model-control-mode explicit so the actual model can be loaded afterwards
        if self.client.is_model_ready(model_name):
            return

        self.client.load_model(model_name)
        t0 = time.perf_counter()
        t1 = t0
        while not self.client.is_model_ready(model_name) and t1 - t0 < timeout:
            t1 = time.perf_counter()
        if not self.client.is_model_ready(model_name):
            raise RuntimeError(f"Failed to load {model_name} on Triton in {timeout}s")

    def get_model_list(self) -> List[str]:
        """Get a list of models loaded in the triton server."""
        res = self.client.get_model_repository_index(as_json=True)
        return [model["name"] for model in res["models"]]

    def get_model_concurrency(self, model_name: str, timeout: int = 1000) -> int:
        """Get the modle concurrency."""
        self.load_model(model_name, timeout)
        instances = self.client.get_model_config(model_name, as_json=True)["config"][
            "instance_group"
        ]
        return sum(instance["count"] * len(instance["gpus"]) for instance in instances)

    @staticmethod
    def process_result(result: Dict[str, str]) -> Dict[str, str]:
        """Post-process the result from the server."""
        import google.protobuf.json_format  # pylint: disable=import-outside-toplevel
        import tritonclient.grpc as grpcclient  # pylint: disable=import-outside-toplevel

        # pylint: disable-next=import-outside-toplevel
        from tritonclient.grpc.service_pb2 import ModelInferResponse

        message = ModelInferResponse()
        google.protobuf.json_format.Parse(json.dumps(result), message)
        infer_result = grpcclient.InferResult(message)
        np_res = infer_result.as_numpy("OUTPUT_0")
        if np_res.ndim == 2:
            generated_text = np_res[0, 0].decode()
        else:
            generated_text = np_res[0].decode()

        return {
            "OUTPUT_0": generated_text,
        }

    def stream_callback(
        self,
        result_queue: queue.Queue[Union[Optional[Dict[str, str]], str]],
        result: Any,
        error: str,
    ) -> None:
        """Add streamed result to queue."""
        if error:
            result_queue.put(error)
        else:
            response = result.get_response(as_json=True)
            if "outputs" in response:
                # the very last response might have no output, just the final flag
                result_queue.put(self.process_result(response))

            if response["parameters"]["triton_final_response"]["bool_param"]:
                # end of the generation
                result_queue.put(None)

    def send_prompt_streaming(
        self,
        model_name: str,
        request_inputs: Any,
        request_outputs: Optional[Any],
        result_queue: queue.Queue[Union[Optional[Dict[str, str]], str]],
    ) -> None:
        """Send the prompt and start streaming the result."""
        self.client.start_stream(callback=partial(self.stream_callback, result_queue))
        self.client.async_stream_infer(
            model_name=model_name,
            inputs=request_inputs,
            outputs=request_outputs,
            enable_empty_final_response=True,
        )

    def request_streaming(
        self,
        model_name: str,
        result_queue: queue.Queue[Union[Optional[Dict[str, str]], str]],
        **params: Any,
    ) -> None:
        """Request a streaming connection."""
        if not self.client.is_model_ready(model_name):
            raise RuntimeError("Cannot request streaming, model is not loaded")
        inputs = self.generate_inputs(**params)
        outputs = self.generate_outputs()
        self.send_prompt_streaming(model_name, inputs, outputs, result_queue)

    def close_streaming(self) -> None:
        """Close the streaming connection."""
        # unfortunately we can't do it inside the callback
        self.client.stop_stream()

    @staticmethod
    def generate_outputs() -> List["grpcclient.InferRequestedOutput"]:
        """Generate the expected output structure."""
        import tritonclient.grpc as grpcclient  # pylint: disable=import-outside-toplevel

        return [grpcclient.InferRequestedOutput("OUTPUT_0")]

    @staticmethod
    def prepare_tensor(name: str, input_data: Any) -> "grpcclient.InferInput":
        """Prepare an input data structure."""
        import tritonclient.grpc as grpcclient  # pylint: disable=import-outside-toplevel

        # pylint: disable-next=import-outside-toplevel
        from tritonclient.utils import np_to_triton_dtype

        t = grpcclient.InferInput(
            name, input_data.shape, np_to_triton_dtype(input_data.dtype)
        )
        t.set_data_from_numpy(input_data)
        return t

    @staticmethod
    def generate_inputs(  # pylint: disable=too-many-arguments,too-many-locals
        prompt: str,
        tokens: int = 32,
        temperature: float = 0.5,
        top_k: float = 0,
        top_p: float = 0.9,
        beam_width: int = 1,
        repetition_penalty: float = 1,
        length_penalty: float = 1.0,
    ) -> List["grpcclient.InferInput"]:
        """Create the input for the triton inference server."""
        import numpy as np  # pylint: disable=import-outside-toplevel

        # stop = STOP_WORDS
        # bad = BAD_WORDS

        query = np.array(prompt).astype(object)
        request_output_len = np.array([tokens]).astype(np.uint32).reshape((1, -1))
        runtime_top_k = np.array([top_k]).astype(np.uint32).reshape((1, -1))
        runtime_top_p = np.array([top_p]).astype(np.float32).reshape((1, -1))
        temperature_array = np.array([temperature]).astype(np.float32).reshape((1, -1))
        len_penalty = np.array([length_penalty]).astype(np.float32).reshape((1, -1))
        repetition_penalty_array = (
            np.array([repetition_penalty]).astype(np.float32).reshape((1, -1))
        )
        random_seed = np.array([RANDOM_SEED]).astype(np.uint64).reshape((1, -1))
        beam_width_array = np.array([beam_width]).astype(np.uint32).reshape((1, -1))
        streaming_data = np.array([[True]], dtype=bool)

        inputs = [
            TritonClient.prepare_tensor("INPUT_0", query),
            TritonClient.prepare_tensor("INPUT_1", request_output_len),
            TritonClient.prepare_tensor("runtime_top_k", runtime_top_k),
            TritonClient.prepare_tensor("runtime_top_p", runtime_top_p),
            TritonClient.prepare_tensor("temperature", temperature_array),
            TritonClient.prepare_tensor("len_penalty", len_penalty),
            TritonClient.prepare_tensor("repetition_penalty", repetition_penalty_array),
            TritonClient.prepare_tensor("random_seed", random_seed),
            TritonClient.prepare_tensor("beam_width", beam_width_array),
            TritonClient.prepare_tensor("streaming", streaming_data),
        ]
        return inputs
