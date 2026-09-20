from django import forms
from django.contrib.auth import password_validation

from apps.accounts.models import Membership, Role, User


class TailwindMixin:
    """Applies the shared input styling without repeating it in every template."""

    default_class = "input"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
                continue
            existing = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{existing} {self.default_class}".strip()


class SignupForm(TailwindMixin, forms.Form):
    """
    One form creates the person and the business together.

    A shop owner signing up has no interest in the distinction, and splitting
    it across two screens is where trial signups are lost.
    """

    business_name = forms.CharField(
        label="Business name", max_length=120,
        widget=forms.TextInput(attrs={"placeholder": "Duka la Salma"}),
    )
    name = forms.CharField(
        label="Your name", max_length=120,
        widget=forms.TextInput(attrs={"placeholder": "Salma Juma"}),
    )
    email = forms.EmailField(label="Email")
    phone = forms.CharField(label="Phone", max_length=30, required=False)
    password = forms.CharField(label="Password", widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists. Sign in instead."
            )
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password


class RoleForm(TailwindMixin, forms.ModelForm):
    class Meta:
        model = Role
        fields = ["name", "description"]
        widgets = {
            "description": forms.TextInput(
                attrs={"placeholder": "What this role is for"}
            ),
        }

    def __init__(self, *args, tenant=None, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        existing = Role.objects.filter(name__iexact=name)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("A role with this name already exists.")
        return name


class StaffForm(TailwindMixin, forms.Form):
    """Invite or edit a staff member: who they are, their role, their shops."""

    name = forms.CharField(label="Name", max_length=120)
    email = forms.EmailField(label="Email")
    phone = forms.CharField(label="Phone", max_length=30, required=False)
    role = forms.ModelChoiceField(label="Role", queryset=Role.objects.none())
    all_branches = forms.BooleanField(
        label="Works in every branch", required=False, initial=True,
        help_text="Including branches opened later. Untick to choose branches below.",
    )
    branches = forms.ModelMultipleChoiceField(
        label="Works at",
        queryset=None,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Only used when \"every branch\" is unticked. Owners always cover all.",
    )
    password = forms.CharField(
        label="Password",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Set it here and tell them in person. Leave blank when editing to keep the current one.",
    )
    pin = forms.CharField(
        label="Approval PIN",
        max_length=8,
        required=False,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric"}),
        help_text="Only for staff who approve other people's actions at the till.",
    )

    def __init__(self, *args, tenant=None, instance=None, actor=None, **kwargs):
        from apps.accounts import guards
        from apps.org.models import Branch

        self.tenant = tenant
        self.instance = instance
        self.actor = actor
        super().__init__(*args, **kwargs)
        roles = Role.objects.all()
        # Only an owner hands out Owner -- and a non-owner editing an owner
        # still has to see the role that owner holds.
        if actor is not None and not guards.is_owner(actor):
            roles = roles.exclude(is_owner_role=True)
            if instance is not None and instance.role.is_owner_role:
                roles = Role.objects.filter(pk=instance.role_id) | roles
        self.fields["role"].queryset = roles
        if instance is not None:
            # Locked fields are read from `initial`, not the submitted data,
            # so they must carry the person's current values.
            self.fields["email"].initial = instance.user.email
            self.fields["name"].initial = instance.user.name
            self.fields["phone"].initial = instance.user.phone
            # Their sign-in name, shared with every other shop they work in.
            # It was shown as editable and then silently ignored.
            self.fields["email"].disabled = True
            self.fields["email"].help_text = "How they sign in. They can change it only through support."
        if instance is not None and not instance.user.shop_may_set_password(tenant):
            # Their account is used elsewhere too -- another shop, or the
            # platform. Their name and password are theirs, not this shop's.
            for name in ("name", "phone", "password"):
                self.fields[name].disabled = True
            self.fields["password"].help_text = (
                "They also use this account elsewhere, so only they can change it."
            )
        self.fields["branches"].queryset = Branch.objects.filter(is_active=True)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        clash = Membership.objects.filter(user__email=email)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("This person is already on your team.")
        return email

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if password:
            password_validation.validate_password(password)
        return password

    def clean_pin(self):
        # Typed at the till in front of customers: digits only, 4 to 8 long.
        # Anything was accepted before, including a one-digit PIN.
        pin = (self.cleaned_data.get("pin") or "").strip()
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise forms.ValidationError("A PIN is 4 to 8 digits.")
        return pin

    def clean_role(self):
        from apps.accounts import guards

        role = self.cleaned_data["role"]
        if (self.actor is not None and not guards.role_fits(self.actor, role)
                and not (self.instance is not None and self.instance.role_id == role.pk)):
            raise forms.ValidationError(
                f"{role.name} can do things you cannot, so you cannot give it out."
            )
        return role

    def clean(self):
        from apps.accounts import guards

        data = super().clean()
        if not data.get("all_branches") and not data.get("branches"):
            self.add_error("branches", "Tick at least one branch, or \"every branch\".")
        elif self.actor is not None and not guards.branches_fit(
            self.actor, all_branches=data.get("all_branches"),
            branches=data.get("branches") or [],
        ):
            self.add_error("branches", "You can only place people in branches you work in yourself.")
        # A new person needs a way in, or they could never sign in at all. A
        # person who already has an account keeps the password they chose.
        if self.instance is None and not data.get("password") and data.get("email"):
            from apps.accounts.models import User

            if not User.objects.filter(email=data["email"]).exists():
                self.add_error("password", "A new person needs a password to sign in with.")
        return data
