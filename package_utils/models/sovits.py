import gc
import json
import os
import pickle

import yaml
from SoVITS.compress_model import copyStateDict
from SoVITS.models import SynthesizerTrn
from fap.utils.file import make_dirs
from package_utils.exec import executable
import time

import torch
import torchaudio
from SoVITS.inference import infer_tool
from SoVITS.inference.infer_tool import Svc
from package_utils.config import JSONReader, YAMLReader, applyChanges
from package_utils.const_vars import WORK_DIR_PATH
from package_utils.dataset_utils import auto_normalize_dataset
from package_utils.exec import exec, start_with_cmd
from package_utils.model_utils import load_pretrained
from package_utils.ui.FormTypes import FormDictInModelClass
from .common import common_infer_form, common_preprocess_form
from SoVITS import utils
import gradio as gr


import soundfile as sf


class SoVITSModel:
    model_name = "So-VITS-SVC"

    def get_config_main(*args):
        with JSONReader("configs/sovits.json") as config:
            if config["train"].get("num_workers", None) is None:
                config["train"]["num_workers"] = 2
            return config

    def get_config_diff(*args):
        with YAMLReader("configs/sovits_diff.yaml") as config:
            return config

    _infer_form: FormDictInModelClass = {
        "cluster_infer_ratio": {
            "type": "slider",
            "max": 1,
            "min": 0,
            "default": 0,
            "step": 0.1,
            "label": "聚类/特征比例",
            "info": "聚类/特征占比，范围0-1，若没有训练聚类模型或特征检索则默认0即可",
        },
        "linear_gradient": {
            "type": "slider",
            "info": "两段音频切片的交叉淡入长度",
            "label": "渐变长度",
            "default": 0,
            "min": 0,
            "max": 1,
            "step": 0.1,
        },
        "k_step": {
            "type": "slider",
            "max": 1000,
            "min": 1,
            "default": 100,
            "step": 1,
            "label": "扩散步数",
            "info": "越大越接近扩散模型的结果，默认100",
        },
        "enhancer_adaptive_key": {
            "type": "slider",
            "max": 12,
            "min": -12,
            "step": 1,
            "default": 0,
            "label": "增强器适应",
            "info": "使增强器适应更高的音域(单位为半音数)|默认为0",
        },
        "f0_filter_threshold": {
            "type": "slider",
            "max": 1,
            "min": 0,
            "default": 0.05,
            "step": 0.01,
            "label": "f0 过滤阈值",
            "info": "只有使用crepe时有效. 数值范围从0-1. 降低该值可减少跑调概率，但会增加哑音",
        },
        "audio_predict_f0": {
            "type": "checkbox",
            "default": False,
            "info": "语音转换自动预测音高，转换歌声时不要打开这个会严重跑调",
            "label": "自动 f0 预测",
        },
        "second_encoding": {
            "type": "checkbox",
            "default": False,
            "label": "二次编码",
            "info": "浅扩散前会对原始音频进行二次编码，玄学选项，有时候效果好，有时候效果差",
        },
        "clip": {
            "type": "slider",
            "max": 100,
            "min": 0,
            "default": 0,
            "step": 1,
            "label": "强制切片长度",
            "info": "强制音频切片长度, 0 为不强制",
        },
    }

    train_form = {}

    _preprocess_form = {
        "use_diff": {
            "type": "checkbox",
            "default": False,
            "label": "训练浅扩散",
            "info": "勾选后将会生成训练浅扩散需要的文件，会比不选慢",
        }
    }

    model_types = {
        "main": "主模型",
        "diff": "浅扩散模型",
        "cluster": "聚类/检索模型",
    }

    model_chooser_extra_form = {
        "enhance": {
            "type": "checkbox",
            "default": False,
            "label": "NSFHifigan 音频增强",
            "info": "对部分训练集少的模型有一定的音质增强效果，对训练好的模型有反面效果",
        },
        "feature_retrieval": {
            "type": "checkbox",
            "default": False,
            "label": "启用特征提取",
            "info": "是否使用特征检索，如果使用聚类模型将被禁用",
        },
    }

    def install_model(self, package, model_name):
        model_dict = package["model_dict"]
        config_dict = package["config_dict"]
        base_path = os.path.join("models", model_name)
        make_dirs(base_path)

        if model_dict.get("main", None):
            torch.save(model_dict["main"], os.path.join(base_path, "model.pth"))
            # 将 config_dict["main"] 保存为 config.json
            with open(os.path.join(base_path, "config.json"), "w") as f:
                json.dump(config_dict["main"], f)

        if model_dict.get("diff", None):
            torch.save(model_dict["diff"], os.path.join(base_path, "diff_model.pt"))
            with open(os.path.join(base_path, "config.yaml"), "w") as f:
                yaml.dump(config_dict["diff"], f)

        if model_dict.get("cluster", None):
            if model_dict["cluster"]["type"] == "index":
                pickle.dump(
                    model_dict["cluster"]["model"],
                    open(os.path.join(base_path, "feature_and_index.pkl"), "wb"),
                )
            else:
                torch.save(
                    model_dict["cluster"]["model"],
                    os.path.join(base_path, "kmeans_10000.pt"),
                )

    def removeOptimizer(self, config: str, input_model: dict, ishalf: bool):
        hps = utils.get_hparams_from_file(config)

        net_g = SynthesizerTrn(
            hps.data.filter_length // 2 + 1,
            hps.train.segment_size // hps.data.hop_length,
            **hps.model,
        )

        optim_g = torch.optim.AdamW(
            net_g.parameters(),
            hps.train.learning_rate,
            betas=hps.train.betas,
            eps=hps.train.eps,
        )

        state_dict_g = input_model
        new_dict_g = copyStateDict(state_dict_g)
        keys = []
        for k, v in new_dict_g["model"].items():
            if "enc_q" in k:
                continue  # noqa: E701
            keys.append(k)

        new_dict_g = (
            {k: new_dict_g["model"][k].half() for k in keys}
            if ishalf
            else {k: new_dict_g["model"][k] for k in keys}
        )

        return {
            "model": new_dict_g,
            "iteration": 0,
            "optimizer": optim_g.state_dict(),
            "learning_rate": 0.0001,
        }

    def pack_model(self, model_dict):
        print(model_dict)
        result = {}
        result["model_dict"] = {}
        result["config_dict"] = {}
        if model_dict.get("main", None):
            # return result["main"]
            config_path = os.path.dirname(model_dict["main"]) + "/config.json"
            result["model_dict"]["main"] = self.removeOptimizer(
                config_path, torch.load(model_dict["main"], map_location="cpu"), True
            )
            with JSONReader(config_path) as config:
                result["config_dict"]["main"] = config

        if model_dict.get("diff", None):
            result["model_dict"]["diff"] = torch.load(
                model_dict["diff"], map_location="cpu"
            )
            config_path = os.path.dirname(model_dict["diff"]) + "/config.yaml"
            with YAMLReader(config_path) as config:
                result["config_dict"]["diff"] = config

        if model_dict.get("cluster", None):
            if model_dict["cluster"].endswith(".pkl"):
                result["model_dict"]["cluster"] = {
                    "type": "index",
                    "model": pickle.load(open(model_dict["cluster"], "rb")),
                }
            else:
                result["model_dict"]["cluster"] = {
                    "type": "cluster",
                    "model": torch.load(model_dict["cluster"], map_location="cpu"),
                }
        return result

    def model_filter(self, filepath: str):
        if (
            filepath.endswith(".pth")
            and not filepath.startswith("D_")
            and not filepath.startswith("G_0")
        ):
            return "main"
        if os.path.basename(filepath) in ["feature_and_index.pkl", "kmeans_10000.pt"]:
            return "cluster"
        if filepath.endswith(".pt"):
            return "diff"

    def unload_model(self):
        if self.svc_model:
            del self.svc_model
        self.svc_model = None
        torch.cuda.empty_cache()
        gc.collect()

    def load_model(self, args):
        print(args)

        main_path = args["main"]
        cluster_path = args["cluster"]
        self.use_cluster = bool(cluster_path)
        if not self.use_cluster:
            cluster_path = ""

        diffusion_model_path = args["diff"]
        self.use_diff = bool(diffusion_model_path)
        if not self.use_diff:
            diffusion_model_path = ""

        device = args["device"]

        if bool(diffusion_model_path):
            diff_config_path = os.path.dirname(diffusion_model_path) + "/config.yaml"
            if not os.path.exists(diff_config_path):
                diff_config_path = (
                    os.path.dirname(diffusion_model_path) + "/diffusion/config.yaml"
                )
        else:
            diff_config_path = None
        self.svc_model = Svc(
            net_g_path=main_path,
            config_path=os.path.dirname(main_path) + "/config.json",
            device=device,
            cluster_model_path=cluster_path,
            nsf_hifigan_enhance=args["enhance"],
            diffusion_model_path=diffusion_model_path,
            diffusion_config_path=diff_config_path,
            shallow_diffusion=self.use_diff,
            only_diffusion=False,
            spk_mix_enable=False,
            feature_retrieval=args["feature_retrieval"],
        )

        with JSONReader(os.path.dirname(main_path) + "/config.json") as config:
            return list(config["spk"].keys())

    def train(self, params, progress: gr.Progress):
        print(params)
        sub_model_name = params["_model_name"]
        if sub_model_name == "So-VITS-SVC - 主模型":
            working_config_path = os.path.join(WORK_DIR_PATH, "config.json")

            config = applyChanges(
                working_config_path,
                params,
            )

            load_pretrained("sovits", config["model"]["speech_encoder"])

            start_with_cmd(
                f"{executable} -m SoVITS.train -c {working_config_path} -m workdir"
            )
        elif sub_model_name == "So-VITS-SVC - 浅扩散模型":
            working_config_path = os.path.join(
                WORK_DIR_PATH, "diffusion", "config.yaml"
            )

            config = applyChanges(
                working_config_path,
                params,
            )

            load_pretrained("sovits_diff", config["data"]["encoder"])

            start_with_cmd(
                f"{executable} -m SoVITS.train_diff -c {working_config_path}"
            )
        elif sub_model_name == "So-VITS-SVC - 聚类/检索模型":
            if params["cluster_or_index"] == "cluster":
                cmd = f"{executable} -m SoVITS.cluster.train_cluster --dataset data/44k"
                if params["use_gpu"]:
                    cmd += " --gpu"
                start_with_cmd(cmd)
            else:
                working_config_path = os.path.join(WORK_DIR_PATH, "config.json")
                start_with_cmd(
                    f"{executable} -m SoVITS.train_index --root_dir data/44k     -c {working_config_path}"
                )

    def preprocess(self, params, progress=gr.Progress()):
        # aa
        with open("data/model_type", "w") as f:
            f.write("2")
        auto_normalize_dataset("data/44k", False, progress)
        exec(
            f"{executable} -m SoVITS.preprocess_flist_config --source_dir ./data/44k --speech_encoder {params['encoder'].replace('contentvec','vec')}"
        )
        exec(
            f"{executable} -m SoVITS.preprocess_new --f0_predictor {params['f0']} --num_processes 4 {'--use_diff' if params['use_diff'] else ''}"
        )
        return "完成"

    def infer(self, params, progress=gr.Progress()):
        wf, sr = torchaudio.load(params["audio"])
        # 重采样到单声道44100hz 保存到 tmp/时间戳_md5前3位.wav
        resampled_filename = f"tmp/{int(time.time())}.wav"
        torchaudio.save(
            uri=resampled_filename,
            src=torchaudio.functional.resample(
                waveform=wf, orig_freq=sr, new_freq=44100
            ),
            sample_rate=sr,
        )

        kwarg = {
            "raw_audio_path": resampled_filename,
            "spk": params["spk"],
            "tran": params["keychange"],
            "slice_db": params["threshold"],
            "cluster_infer_ratio": params["cluster_infer_ratio"]
            if self.use_cluster
            else 0,
            "auto_predict_f0": params["audio_predict_f0"],
            "noice_scale": 0.4,
            "pad_seconds": 0.5,
            "clip_seconds": params["clip"],
            "lg_num": params["linear_gradient"],
            "lgr_num": 0.75,
            "f0_predictor": params["f0"],
            "enhancer_adaptive_key": params["enhancer_adaptive_key"],
            "cr_threshold": params["f0_filter_threshold"],
            "k_step": params["k_step"],
            "use_spk_mix": False,
            "second_encoding": params["second_encoding"],
            "loudness_envelope_adjustment": 1,
        }
        infer_tool.format_wav(params["audio"])
        self.svc_model.audio16k_resample_transform = torchaudio.transforms.Resample(
            self.svc_model.target_sample, 16000
        ).to(self.svc_model.dev)
        audio = self.svc_model.slice_inference(**kwarg)
        gc.collect()
        torch.cuda.empty_cache()
        sf.write("tmp.wav", audio, 44100)
        # 删掉 filename
        os.remove(resampled_filename)
        print(params)
        return "tmp.wav"

    def __init__(self) -> None:
        self.infer_form = {}
        self.infer_form.update(common_infer_form)
        self.infer_form.update(self._infer_form)

        self.preprocess_form = {}
        self.preprocess_form.update(self._preprocess_form)
        self.preprocess_form.update(common_preprocess_form)

        self.train_form.update(
            {
                "main": {
                    "train.log_interval": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "log_interval"
                        ],
                        "label": "日志间隔",
                        "info": "每 N 步输出一次日志",
                        "max": 10000,
                        "min": 1,
                        "step": 1,
                    },
                    "train.eval_interval": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "eval_interval"
                        ],
                        "label": "验证间隔",
                        "info": "每 N 步保存一次并验证",
                        "max": 10000,
                        "min": 1,
                        "step": 1,
                    },
                    "train.all_in_mem": {
                        "type": "dropdown_liked_checkbox",
                        "default": lambda: self.get_config_main()["train"][
                            "all_in_mem"
                        ],
                        "label": "缓存全数据集",
                        "info": "将所有数据集加载到内存中训练，会加快训练速度，但是需要足够的内存",
                    },
                    "train.keep_ckpts": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "keep_ckpts"
                        ],
                        "label": "保留检查点",
                        "info": "保留最近 N 个检查点",
                        "max": 100,
                        "min": 1,
                        "step": 1,
                    },
                    "train.batch_size": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "batch_size"
                        ],
                        "label": "训练批次大小",
                        "info": "越大越好，越大越占显存",
                        "max": 1000,
                        "min": 1,
                        "step": 1,
                    },
                    "train.learning_rate": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "learning_rate"
                        ],
                        "label": "学习率",
                        "info": "学习率",
                        "max": 1,
                        "min": 0,
                        "step": 0.00001,
                    },
                    "train.num_workers": {
                        "type": "slider",
                        "default": lambda: self.get_config_main()["train"][
                            "num_workers"
                        ],
                        "label": "数据加载器进程数",
                        "info": "仅在 CPU 核心数大于 4 时启用，遵循大就是好原则",
                        "max": 10,
                        "min": 1,
                        "step": 1,
                    },
                },
                "diff": {
                    "train.batchsize": {
                        "type": "slider",
                        "default": lambda: self.get_config_diff()["train"][
                            "batch_size"
                        ],
                        "label": "训练批次大小",
                        "info": "越大越好，越大越占显存，注意不能超过训练集条数",
                        "max": 9999,
                        "min": 1,
                        "step": 1,
                    },
                    "train.num_workers": {
                        "type": "slider",
                        "default": lambda: self.get_config_diff()["train"][
                            "num_workers"
                        ],
                        "label": "训练进程数",
                        "info": "如果你显卡挺好，可以设为 0",
                        "max": 9999,
                        "min": 0,
                        "step": 1,
                    },
                    "train.amp_dtype": {
                        "type": "dropdown",
                        "default": lambda: self.get_config_diff()["train"]["amp_dtype"],
                        "label": "训练精度",
                        "info": "选择 fp16、bf16 可以获得更快的速度，但是炸炉概率 up up",
                        "choices": ["fp16", "bf16", "fp32"],
                    },
                    "train.lr": {
                        "type": "slider",
                        "default": lambda: self.get_config_diff()["train"]["lr"],
                        "step": 0.00001,
                        "min": 0.00001,
                        "max": 0.1,
                        "label": "学习率",
                        "info": "不建议动",
                    },
                    "train.interval_val": {
                        "type": "slider",
                        "default": lambda: self.get_config_diff()["train"][
                            "interval_val"
                        ],
                        "label": "验证间隔",
                        "info": "每 N 步验证一次，同时保存",
                        "max": 10000,
                        "min": 1,
                        "step": 1,
                    },
                    "train.interval_log": {
                        "type": "slider",
                        "default": lambda: self.get_config_diff()["train"][
                            "interval_log"
                        ],
                        "label": "日志间隔",
                        "info": "每 N 步输出一次日志",
                        "max": 10000,
                        "min": 1,
                        "step": 1,
                    },
                    "train.interval_force_save": {
                        "type": "slider",
                        "label": "强制保存模型间隔",
                        "info": "每 N 步保存一次模型",
                        "min": 0,
                        "max": 100000,
                        "default": lambda: self.get_config_diff()["train"][
                            "interval_force_save"
                        ],
                        "step": 1000,
                    },
                    "train.gamma": {
                        "type": "slider",
                        "label": "lr 衰减力度",
                        "info": "不建议动",
                        "min": 0,
                        "max": 1,
                        "default": lambda: self.get_config_diff()["train"]["gamma"],
                        "step": 0.1,
                    },
                    "train.cache_device": {
                        "type": "dropdown",
                        "label": "缓存设备",
                        "info": "选择 cuda 可以获得更快的速度，但是需要更大显存的显卡 (SoVITS 主模型无效)",
                        "choices": ["cuda", "cpu"],
                        "default": lambda: self.get_config_diff()["train"][
                            "cache_device"
                        ],
                    },
                    "train.cache_all_data": {
                        "type": "dropdown_liked_checkbox",
                        "label": "缓存所有数据",
                        "info": "可以获得更快的速度，但是需要大内存/显存的设备",
                        "default": lambda: self.get_config_diff()["train"][
                            "cache_all_data"
                        ],
                    },
                    "train.epochs": {
                        "type": "slider",
                        "label": "最大训练轮数",
                        "info": "达到设定值时将会停止训练",
                        "min": 50000,
                        "max": 1000000,
                        "default": lambda: self.get_config_diff()["train"]["epochs"],
                        "step": 1,
                    },
                    "use_pretrain": {
                        "type": "dropdown_liked_checkbox",
                        "label": "使用预训练模型",
                        "info": "勾选可以大幅减少训练时间，如果你不懂，不要动",
                        "default": True,
                    },
                },
                "cluster": {
                    "cluster_or_index": {
                        "type": "dropdown",
                        "label": "聚类或检索",
                        "info": "要训练聚类还是检索模型，检索咬字比聚类稍好",
                        "choices": ["cluster", "index"],
                        "default": "cluster",
                    },
                    "use_gpu": {
                        "type": "dropdown_liked_checkbox",
                        "label": "使用 GPU",
                        "info": "使用 GPU 可以加速训练，该参数只聚类可用",
                        "default": True,
                    },
                },
            },
        )

        self.svc_model = None