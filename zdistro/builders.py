"""Language builds fill a private build tree before package file mapping."""

from .model import DistroError


def build_language(ctx):
    config = ctx.manifest.data.get("build", {})
    kind = config.get("kind", "none")
    if kind == "none":
        return
    if kind == "cargo":
        command = ["cargo", "build", "--release", "--manifest-path", ctx.project / "Cargo.toml",
                   "--target-dir", ctx.build / "cargo"]
        if config.get("package"):
            command.extend(["--package", config["package"]])
        if config.get("target"):
            command.extend(["--target", config["target"]])
        ctx.run(command, ctx.project)
    elif kind == "python":
        command = [ctx.variables["python"], "-m", "build", "--wheel", "--sdist", "--outdir", ctx.build / "python"]
        if config.get("isolation", True) is False:
            command.append("--no-isolation")
        ctx.run([*command, ctx.project], ctx.project)
    elif kind == "npm":
        if config.get("install", False):
            ctx.run(["npm", "ci", "--ignore-scripts"], ctx.project)
        ctx.run(["npm", "run", config.get("script", "build")], ctx.project)
    elif kind == "command":
        ctx.runner.commands(config["commands"], ctx.variables, ctx.project, ctx.env)
    else:
        raise DistroError(f"unknown language builder: {kind}")
